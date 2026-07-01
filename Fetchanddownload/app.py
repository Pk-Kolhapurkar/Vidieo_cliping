import requests
from bs4 import BeautifulSoup
import os
import time
import json
import re
from flask import Flask, request, jsonify

# ==================== CONFIGURATION ====================
# Load API configuration from environment variables.
APIFY_API_TOKEN = os.getenv("APIFY_API_TOKEN")
APIFY_ACTOR_ID = os.getenv("APIFY_ACTOR_ID")

if not APIFY_API_TOKEN:
    print("Warning: APIFY_API_TOKEN environment variable is not set. Apify requests will fail without it.")
if not APIFY_ACTOR_ID:
    print("Warning: APIFY_ACTOR_ID environment variable is not set. Default actor may be required.")

app = Flask(__name__)

# ==================== HELPER FUNCTIONS ====================

def clean_youtube_url(url):
    """Clean and validate YouTube URL."""
    url = url.strip()
    
    # Handle various YouTube URL formats
    patterns = [
        r'(?:youtube\.com\/watch\?v=)([\w-]+)',
        r'(?:youtu\.be\/)([\w-]+)',
        r'(?:youtube\.com\/embed\/)([\w-]+)',
        r'(?:youtube\.com\/v\/)([\w-]+)',
        r'(?:youtube\.com\/shorts\/)([\w-]+)'
    ]
    
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            video_id = match.group(1)
            return f"https://www.youtube.com/watch?v={video_id}"
    
    return url

def extract_video_id(url):
    """Extract video ID from a YouTube URL."""
    patterns = [
        r'v=([\w-]+)',
        r'youtu\.be/([\w-]+)',
        r'embed/([\w-]+)',
        r'shorts/([\w-]+)'
    ]
    
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None

def is_valid_youtube_url(url):
    """Check if a URL is a valid YouTube URL."""
    patterns = [
        r'^https?://(?:www\.)?youtube\.com/watch\?v=[\w-]+',
        r'^https?://(?:www\.)?youtu\.be/[\w-]+',
        r'^https?://(?:www\.)?youtube\.com/embed/[\w-]+',
        r'^https?://(?:www\.)?youtube\.com/shorts/[\w-]+'
    ]
    
    for pattern in patterns:
        if re.match(pattern, url):
            return True
    return False

# ==================== SCRAPING FUNCTIONS ====================

def get_top_videos():
    """Scrape top 10 most viewed videos from Kworb.net."""
    url = 'https://kworb.net/youtube/'
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }
    
    try:
        print("📡 Fetching data from Kworb.net...")
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.content, 'html.parser')
        table = soup.find('table', id='youtuberealtime')
        
        if not table:
            print("❌ Could not find the table with video data.")
            return []
        
        rows = table.find('tbody').find_all('tr')
        top_videos = []
        
        for row in rows[:10]:
            cols = row.find_all('td')
            if len(cols) < 5:
                continue
                
            rank = cols[0].get_text(strip=True)
            video_cell = cols[2]
            kworb_link_tag = video_cell.find('a')
            
            if not kworb_link_tag:
                continue
                
            kworb_link = kworb_link_tag['href']
            video_title = kworb_link_tag.get_text(strip=True)
            
            video_id = kworb_link.split('/')[-1].replace('.html', '')
            youtube_url = f'https://www.youtube.com/watch?v={video_id}'
            
            views = cols[3].get_text(strip=True)
            likes = cols[4].get_text(strip=True)
            
            top_videos.append({
                'rank': rank,
                'title': video_title,
                'video_id': video_id,
                'youtube_url': youtube_url,
                'views': views,
                'likes': likes
            })
        
        print(f"✅ Successfully extracted {len(top_videos)} videos")
        return top_videos
        
    except Exception as e:
        print(f"❌ Error fetching data: {e}")
        return []

# ==================== APIFY DOWNLOAD FUNCTIONS ====================

def download_video_apify(youtube_url, output_filename='output.mp4'):
    """
    Download a YouTube video using Apify's API with correct input format.
    """
    try:
        # Clean the URL
        cleaned_url = clean_youtube_url(youtube_url)
        print(f"\n📥 URL: {cleaned_url}")
        
        if not is_valid_youtube_url(cleaned_url):
            return False, f"Invalid YouTube URL: {cleaned_url}", None
        
        # Extract video ID
        video_id = extract_video_id(cleaned_url)
        print(f"🎬 Video ID: {video_id}")
        
        # CORRECT INPUT FORMAT for streamers~youtube-video-downloader
        # The actor requires "videos" field as an array of objects with "url" property
        input_data = {
            "videos": [
                {
                    "url": cleaned_url
                }
            ]
        }
        
        api_base = "https://api.apify.com/v2"
        headers = {
            'Authorization': f'Bearer {APIFY_API_TOKEN}',
            'Content-Type': 'application/json'
        }
        
        print(f"🔄 Starting actor with input: {json.dumps(input_data, indent=2)}")
        
        start_url = f"{api_base}/actors/{APIFY_ACTOR_ID}/runs"
        response = requests.post(start_url, json=input_data, headers=headers)
        
        if response.status_code != 201:
            error_detail = response.text[:500]
            return False, f"Failed to start actor (status {response.status_code}): {error_detail}", None
        
        run_data = response.json()
        run_id = run_data['data']['id']
        print(f"🔄 Actor run started: {run_id}")
        
        # Wait for completion
        max_attempts = 120  # 10 minutes max
        attempts = 0
        
        while attempts < max_attempts:
            status_url = f"{api_base}/actor-runs/{run_id}"
            status_response = requests.get(status_url, headers=headers)
            
            if status_response.status_code != 200:
                print(f"⚠️ Status check failed: {status_response.status_code}")
                time.sleep(5)
                attempts += 1
                continue
                
            status_data = status_response.json()
            current_status = status_data['data']['status']
            print(f"⏳ Status: {current_status}")
            
            if current_status == 'SUCCEEDED':
                print("✅ Actor run completed successfully")
                break
            elif current_status in ['FAILED', 'ABORTED', 'TIMED-OUT']:
                return False, f"Actor run {current_status}", None
            
            attempts += 1
            time.sleep(5)
        
        if attempts >= max_attempts:
            return False, "Actor run timed out after 10 minutes", None
        
        # Get results from dataset
        dataset_id = status_data['data']['defaultDatasetId']
        results_url = f"{api_base}/datasets/{dataset_id}/items"
        results_response = requests.get(results_url, headers=headers)
        
        if results_response.status_code != 200:
            return False, f"Failed to get results: {results_response.text[:200]}", None
            
        results = results_response.json()
        print("=" * 100)
        print(json.dumps(results, indent=2))
        print("=" * 100)
        
        if not results:
            # Try getting from key-value store as backup
            print("No dataset results, trying key-value store...")
            store_id = status_data['data']['defaultKeyValueStoreId']
            store_url = f"{api_base}/key-value-stores/{store_id}/records"
            store_response = requests.get(store_url, headers=headers)
            
            if store_response.status_code == 200:
                store_data = store_response.json()
                # Look for video files in store
                for key, value in store_data.items():
                    if key.endswith(('.mp4', '.webm', '.mkv')):
                        download_url = f"{api_base}/key-value-stores/{store_id}/records/{key}"
                        print(f"✅ Found video file in store: {key}")
                        
                        # Download the file
                        return download_file_from_url(download_url, output_filename)
            
            return False, "No results returned from actor", None
        
        # Process dataset results
        print(f"📋 Found {len(results)} results")
        
        # Find download URL from results
        video_data = results[0]
        print(f"📋 Response keys: {list(video_data.keys())}")
        
        download_url = None
        
        # Try different field names for download URL
        url_fields = ['downloadUrl', 'videoUrl', 'url', 'download', 'fileUrl', 'link']
        for field in url_fields:
            if field in video_data and video_data[field]:
                download_url = video_data[field]
                print(f"✅ Found URL in field '{field}'")
                break
        
        # Try nested structures
        if not download_url:
            if 'video' in video_data and isinstance(video_data['video'], dict):
                download_url = video_data['video'].get('url') or video_data['video'].get('downloadUrl')
                if download_url:
                    print("✅ Found URL in video object")
            elif 'file' in video_data and isinstance(video_data['file'], dict):
                download_url = video_data['file'].get('url') or video_data['file'].get('downloadUrl')
                if download_url:
                    print("✅ Found URL in file object")
            elif 'result' in video_data and isinstance(video_data['result'], dict):
                download_url = video_data['result'].get('url') or video_data['result'].get('downloadUrl')
                if download_url:
                    print("✅ Found URL in result object")
        
        # If still no URL, check if it's a direct download
        if not download_url:
            # Some actors return the file directly in the dataset
            for key, value in video_data.items():
                if isinstance(value, str) and (value.startswith('http') and 
                    any(ext in value.lower() for ext in ['.mp4', '.webm', '.mkv', '.mp3', '.m4a'])):
                    download_url = value
                    print(f"✅ Found URL in field '{key}'")
                    break
        
        if not download_url:
            # Try key-value store
            store_id = status_data['data']['defaultKeyValueStoreId']
            store_url = f"{api_base}/key-value-stores/{store_id}/records"
            store_response = requests.get(store_url, headers=headers)
            
            if store_response.status_code == 200:
                store_data = store_response.json()
                for key, value in store_data.items():
                    if key.endswith(('.mp4', '.webm', '.mkv')):
                        download_url = f"{api_base}/key-value-stores/{store_id}/records/{key}?attachment=true"
                        print(f"✅ Found video file in store: {key}")
                        break
            
            if not download_url:
                return False, f"No download URL found. Received: {json.dumps(video_data, indent=2)[:500]}", None
        
        # Download the file
        return download_file_from_url(download_url, output_filename)
        
    except Exception as e:
        return False, f"Error: {str(e)}", None

def download_file_from_url(download_url, output_filename):
    """Download a file from URL with progress tracking."""
    try:
        print(f"⬇️ Downloading from: {download_url[:100]}...")
        
        # Try to get filename from URL if not specified
        if output_filename == 'output.mp4' and 'filename=' in download_url:
            filename_match = re.search(r'filename=([^&]+)', download_url)
            if filename_match:
                output_filename = filename_match.group(1)
        
        download_headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'video/webm,video/ogg,video/*;q=0.9,*/*;q=0.8',
        }
        
        # Follow redirects
        file_response = requests.get(download_url, stream=True, headers=download_headers, allow_redirects=True)
        file_response.raise_for_status()
        
        total_size = int(file_response.headers.get('content-length', 0))
        
        with open(output_filename, 'wb') as f:
            downloaded = 0
            for chunk in file_response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total_size:
                        progress = (downloaded / total_size) * 100
                        print(f"⬇️ Progress: {progress:.1f}%", end='\r')
        
        print(f"\n✅ Downloaded: {output_filename}")
        file_size = os.path.getsize(output_filename) / (1024 * 1024)
        print(f"📊 Size: {file_size:.2f} MB")
        print(f"📁 Path: {os.path.abspath(output_filename)}")
        
        return True, f"Download successful: {output_filename}", output_filename
        
    except Exception as e:
        return False, f"Download error: {str(e)}", None

# ==================== MAIN DOWNLOAD FUNCTION ====================

def download_video(youtube_url, output_filename='output.mp4'):
    """Main download function."""
    return download_video_apify(youtube_url, output_filename)

# ==================== FLASK API ENDPOINTS ====================

@app.route('/download_top', methods=['GET'])
def download_top_video():
    """Download the #1 most viewed video from Kworb.net"""
    print("\n🎯 Downloading #1 video from Kworb.net...")
    top_videos = get_top_videos()
    
    if not top_videos:
        return jsonify({"error": "Failed to fetch top videos"}), 500
    
    top_video = top_videos[0]
    print(f"📹 Video: {top_video['title']}")
    print(f"🔗 URL: {top_video['youtube_url']}")
    
    # Create filename from video title
    filename = f"{top_video['title'][:50].replace(' ', '_').replace('/', '_')}.mp4"
    filename = re.sub(r'[^\w\-_.]', '', filename)
    
    success, message, file_path = download_video(
        top_video['youtube_url'], 
        filename
    )
    
    if success:
        return jsonify({
            "success": True,
            "message": message,
            "video": top_video,
            "file_path": file_path,
            "filename": filename
        }), 200
    else:
        return jsonify({"error": message}), 500

@app.route('/download', methods=['POST'])
def download_video_endpoint():
    """Download a specific video"""
    data = request.get_json()
    url = data.get('url')
    output = data.get('output', 'output.mp4')
    
    if not url:
        return jsonify({"error": "Missing 'url' parameter"}), 400
    
    success, message, file_path = download_video(url, output)
    
    if success:
        return jsonify({
            "success": True,
            "message": message,
            "file_path": file_path
        }), 200
    else:
        return jsonify({"error": message}), 500

@app.route('/top_videos', methods=['GET'])
def top_videos_endpoint():
    """Get the top 10 most viewed videos from Kworb.net"""
    top_videos = get_top_videos()
    
    if top_videos:
        return jsonify({
            "success": True,
            "count": len(top_videos),
            "videos": top_videos,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
        }), 200
    else:
        return jsonify({"error": "Failed to fetch top videos"}), 500

@app.route('/test_apify', methods=['GET'])
def test_apify():
    """Test the Apify connection"""
    try:
        test_url = f"https://api.apify.com/v2/actors/{APIFY_ACTOR_ID}"
        headers = {'Authorization': f'Bearer {APIFY_API_TOKEN}'}
        
        test_response = requests.get(test_url, headers=headers)
        
        if test_response.status_code == 200:
            actor_data = test_response.json()
            return jsonify({
                "success": True,
                "message": "Apify connection successful",
                "actor": actor_data.get('data', {}).get('name', 'Unknown'),
                "status": "ready"
            }), 200
        else:
            return jsonify({
                "success": False,
                "error": f"Connection failed with status: {test_response.status_code}"
            }), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/debug_url', methods=['POST'])
def debug_url():
    """Debug endpoint to check URL format"""
    data = request.get_json()
    url = data.get('url', '')
    
    cleaned = clean_youtube_url(url)
    valid = is_valid_youtube_url(cleaned)
    video_id = extract_video_id(cleaned)
    
    return jsonify({
        "original": url,
        "cleaned": cleaned,
        "is_valid": valid,
        "video_id": video_id
    }), 200

@app.route('/debug_input', methods=['GET'])
def debug_input():
    """Debug endpoint to show expected input format"""
    return jsonify({
        "actor_id": APIFY_ACTOR_ID,
        "expected_input_format": {
            "videos": [
                {
                    "url": "https://www.youtube.com/watch?v=VIDEO_ID"
                }
            ]
        },
        "example": {
            "videos": [
                {
                    "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
                }
            ]
        }
    }), 200

@app.route('/', methods=['GET'])
def index():
    return jsonify({
        "service": "YouTube Video Downloader with Apify",
        "version": "1.0.0",
        "actor_id": APIFY_ACTOR_ID,
        "endpoints": {
            "GET /": "This help page",
            "GET /top_videos": "Get top 10 most viewed videos from Kworb.net",
            "GET /download_top": "Download the #1 video from Kworb.net",
            "POST /download": "Download a specific video (body: {'url': '...'})",
            "GET /test_apify": "Test Apify connection",
            "POST /debug_url": "Debug a URL (body: {'url': '...'})",
            "GET /debug_input": "Show expected input format for the actor"
        }
    })

# ==================== MAIN ====================

if __name__ == '__main__':
    os.makedirs('./downloads', exist_ok=True)
    os.chdir('./downloads')  # Change to downloads directory
    
    print("🎬 YouTube Video Downloader with Apify")
    print("=" * 60)
    print(f"📌 Actor: {APIFY_ACTOR_ID}")
    print("📂 Downloads saved to: ./downloads")
    print("=" * 60)
    
    app.run(debug=True, host='0.0.0.0', port=5000)