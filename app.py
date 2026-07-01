import os
import re
import time
import whisper
from moviepy.editor import VideoFileClip, concatenate_videoclips
import requests
import json
import ast

# Helpers

def _extract_balanced_block(text, start_index, open_char, close_char):
    depth = 0
    in_string = False
    escaped = False
    for i in range(start_index, len(text)):
        ch = text[i]
        if ch == "\\" and not escaped:
            escaped = True
            continue
        if ch == '"' and not escaped:
            in_string = not in_string
        if not in_string:
            if ch == open_char:
                depth += 1
            elif ch == close_char:
                depth -= 1
                if depth == 0:
                    return text[start_index:i + 1]
        escaped = False
    raise ValueError("Unbalanced JSON block")


def _extract_json_payload(text):
    text = text.strip()

    last_json_object = None
    for match in re.finditer(r"\{", text):
        try:
            candidate = _extract_balanced_block(text, match.start(), '{', '}')
        except ValueError:
            continue
        if '"conversations"' in candidate or "'conversations'" in candidate:
            last_json_object = candidate

    if last_json_object:
        return last_json_object

    for opener, closer in [('[', ']'), ('{', '}')]:
        for match in reversed(list(re.finditer(re.escape(opener), text))):
            try:
                return _extract_balanced_block(text, match.start(), opener, closer)
            except ValueError:
                continue

    raise ValueError('No JSON payload found in model response')


def _estimate_tokens(text):
    # Rough heuristic: ~4 chars per token for English text.
    return max(1, len(text) // 4)


def _chunk_transcript(transcript, max_tokens_per_chunk=4000):
    """
    Splits the transcript (list of segment dicts) into chunks small enough
    to stay under the model's TPM limit, without breaking a segment apart.
    """
    chunks = []
    current_chunk = []
    current_tokens = 0

    for seg in transcript:
        seg_text = json.dumps(seg)
        seg_tokens = _estimate_tokens(seg_text)

        if current_chunk and current_tokens + seg_tokens > max_tokens_per_chunk:
            chunks.append(current_chunk)
            current_chunk = []
            current_tokens = 0

        current_chunk.append(seg)
        current_tokens += seg_tokens

    if current_chunk:
        chunks.append(current_chunk)

    return chunks


# Step 1: Transcribe the Video
def transcribe_video(video_path, model_name="base"):
    model = whisper.load_model(model_name)
    audio_path = "temp_audio.wav"
    os.system(f"ffmpeg -i {video_path} -ar 16000 -ac 1 -b:a 64k -f mp3 {audio_path}")
    result = model.transcribe(audio_path)
    transcription = []
    for segment in result['segments']:
        transcription.append({
            'start': segment['start'],
            'end': segment['end'],
            'text': segment['text'].strip()
        })
    return transcription


def _call_groq(prompt_transcript, user_query, max_retries=4):
    prompt = f"""You are an expert video editor who can read video transcripts and perform video editing. Given a transcript with segments, your task is to identify all the conversations related to a user query. Follow these guidelines when choosing conversations. A group of continuous segments in the transcript is a conversation.

Guidelines:
1. The conversation should be relevant to the user query. The conversation should include more than one segment to provide context and continuity.
2. Include all the before and after segments needed in a conversation to make it complete.
3. The conversation should not cut off in the middle of a sentence or idea.
4. Choose multiple conversations from the transcript that are relevant to the user query.
5. Match the start and end time of the conversations using the segment timestamps from the transcript.
6. The conversations should be a direct part of the video and should not be out of context.
7. This transcript may be a partial chunk of a longer video. Only use the segments given to you below — do not invent timestamps outside this range. If nothing in this chunk is relevant, return an empty list.

Output format: {{ "conversations": [{{"start": "s1", "end": "e1"}}, {{"start": "s2", "end": "e2"}}] }}

Important: respond with valid JSON only. Do not include any extra text, explanation, or markdown. If you cannot find any relevant conversation, return {{ "conversations": [] }}.

Transcript:
{prompt_transcript}

User query:
{user_query}"""

    url = os.getenv("GROQ_API_URL", "https://api.groq.com/openai/v1/chat/completions")
    groq_api_key = os.getenv("GROQ_API_KEY")
    if not groq_api_key:
        raise RuntimeError("Missing required environment variable: GROQ_API_KEY")

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {groq_api_key}"
    }

    data = {
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": "Return only the JSON object described above. No explanation, no markdown, no extra text."}
        ],
        "model": os.getenv("GROQ_MODEL", "llama-3.1-8b-instant"),
        "temperature": 0.2,
        "max_tokens": 1024,
        "top_p": 1,
        "stream": False,
        "response_format": {"type": "json_object"},
        "stop": None
    }

    for attempt in range(1, max_retries + 1):
        response = requests.post(url, headers=headers, json=data)

        if response.status_code == 429 or response.status_code == 413:
            # Rate limited or too large — back off and retry.
            wait = min(2 ** attempt, 30)
            print(f"Rate limited/too large (status {response.status_code}), retrying in {wait}s...")
            time.sleep(wait)
            continue

        try:
            response_data = response.json()
        except ValueError:
            raise RuntimeError(f"Non-JSON response from API: {response.text}")

        if response.status_code != 200:
            raise RuntimeError(f"API request failed ({response.status_code}): {response_data}")

        if not response_data.get("choices"):
            raise RuntimeError(f"Unexpected API response structure: {response_data}")

        choice = response_data["choices"][0]
        message = choice.get("message") or choice
        raw_content = message.get("content") if isinstance(message, dict) else None
        if raw_content is None:
            raise RuntimeError(f"Missing message content in API response: {response_data}")

        raw_content = raw_content.strip()
        try:
            json_text = _extract_json_payload(raw_content)
        except ValueError as exc:
            raise RuntimeError(f"Could not extract JSON payload from API response: {raw_content}\nError: {exc}")

        try:
            conversations = json.loads(json_text)
        except ValueError:
            try:
                conversations = ast.literal_eval(json_text)
            except Exception as exc:
                raise RuntimeError(f"Could not parse extracted response content as JSON: {json_text}\nError: {exc}")

        if isinstance(conversations, list):
            return conversations

        if not isinstance(conversations, dict) or "conversations" not in conversations:
            raise RuntimeError(f"Parsed API response does not contain conversations: {conversations}")

        return conversations["conversations"]

    raise RuntimeError("Exceeded max retries due to repeated rate limiting.")


def get_relevant_segments(transcript, user_query, max_tokens_per_chunk=4000, delay_between_chunks=2.0):
    chunks = _chunk_transcript(transcript, max_tokens_per_chunk=max_tokens_per_chunk)
    print(f"Transcript split into {len(chunks)} chunk(s) for processing.")

    all_conversations = []
    for i, chunk in enumerate(chunks, start=1):
        print(f"Processing chunk {i}/{len(chunks)} ({len(chunk)} segments)...")
        try:
            conversations = _call_groq(chunk, user_query)
            all_conversations.extend(conversations)
        except RuntimeError as exc:
            print(f"Warning: chunk {i} failed and will be skipped: {exc}")

        if i < len(chunks):
            time.sleep(delay_between_chunks)  # avoid bursting the TPM limit

    return all_conversations


def edit_video(original_video_path, segments, output_video_path, fade_duration=0.5, save_individual_clips=True):
    video = VideoFileClip(original_video_path)
    clips = []

    output_dir = os.path.dirname(output_video_path) or "."
    base_name = os.path.splitext(os.path.basename(output_video_path))[0]

    for i, seg in enumerate(segments, start=1):
        try:
            start = float(seg['start'])
            end = float(seg['end'])
        except (KeyError, ValueError, TypeError):
            print(f"Skipping malformed segment: {seg}")
            continue

        if end <= start:
            print(f"Skipping invalid segment (end <= start): {seg}")
            continue

        clip = video.subclip(start, end).fadein(fade_duration).fadeout(fade_duration)
        clips.append(clip)

        if save_individual_clips:
            individual_path = os.path.join(output_dir, f"{base_name}_clip{i}.mp4")
            clip.write_videofile(individual_path, codec="libx264", audio_codec="aac")
            print(f"Saved individual clip {i}: {individual_path}")

    if clips:
        final_clip = concatenate_videoclips(clips, method="compose")
        final_clip.write_videofile(output_video_path, codec="libx264", audio_codec="aac")
        print(f"Saved merged video: {output_video_path}")
    else:
        print("No segments to include in the edited video.")


# Main Function
def main():
    input_video = "input_video.mp4"
    output_video = "editedddd_output.mp4"

    user_query = "The video explain 20 main ai concepts so create a video that explain all the 20 main ai concepts in summary , video should be of 5 min length"

    print("Transcribing video...")
    transcription = transcribe_video(input_video, model_name="base")

    relevant_segments = get_relevant_segments(transcription, user_query)

    print("Editing video...")
    edit_video(input_video, relevant_segments, output_video)
    print(f"Edited video saved to {output_video}")


if __name__ == "__main__":
    main()