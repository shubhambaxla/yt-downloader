"""
YT Downloader — A premium YouTube video downloader web app.
Uses yt-dlp for downloading and Flask for the backend with SSE progress streaming.
"""

import os
import json
import re
import threading
import time
import uuid
from pathlib import Path

from flask import Flask, render_template, request, jsonify, Response, send_file
from flask_cors import CORS
import yt_dlp

app = Flask(__name__)
CORS(app)

# Explicitly set ffmpeg path for local Windows dev (Docker already has it in PATH)
FFMPEG_DIR = r"C:\Users\shubhu\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1.2-full_build\bin"
if os.path.isdir(FFMPEG_DIR):
    os.environ["PATH"] = FFMPEG_DIR + os.pathsep + os.environ.get("PATH", "")

DOWNLOAD_DIR = Path(__file__).parent / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

COOKIES_FILE = Path(__file__).parent / "cookies.txt"

# In-memory store for download progress
download_tasks = {}


def sanitize_filename(name: str) -> str:
    """Remove characters that are problematic for filenames."""
    name = re.sub(r'[<>:"/\\|?*]', '', name)
    name = name.strip('. ')
    return name[:200]  # limit length


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/cookies", methods=["POST"])
def upload_cookies():
    """Upload a cookies.txt file for YouTube authentication."""
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "Empty filename"}), 400
    f.save(str(COOKIES_FILE))
    return jsonify({"success": True, "message": "Cookies uploaded successfully"})


@app.route("/api/cookies", methods=["GET"])
def cookies_status():
    """Check if a cookies file exists."""
    return jsonify({"hasCookies": COOKIES_FILE.exists()})


@app.route("/api/info", methods=["POST"])
def video_info():
    """Fetch video metadata (title, thumbnail, formats, duration)."""
    data = request.get_json()
    url = data.get("url", "").strip()
    use_cookies = data.get("useCookies", True)
    if not url:
        return jsonify({"error": "URL is required"}), 400

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extractor_args": {"youtube": {"player_client": ["ios", "mweb"]}},
        "http_headers": {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"},
    }
    # Use uploaded cookies file if available, else browser cookies if toggled
    if COOKIES_FILE.exists():
        ydl_opts["cookiefile"] = str(COOKIES_FILE)
    elif use_cookies:
        ydl_opts["cookiesfrombrowser"] = ("chrome",)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        # Build format list — deduplicated, only meaningful ones
        formats_raw = info.get("formats", [])
        seen = set()
        format_list = []

        for f in formats_raw:
            height = f.get("height")
            ext = f.get("ext", "")
            vcodec = f.get("vcodec", "none")
            acodec = f.get("acodec", "none")
            filesize = f.get("filesize") or f.get("filesize_approx") or 0

            if vcodec == "none" and acodec == "none":
                continue

            has_video = vcodec != "none"
            has_audio = acodec != "none"

            if has_video and height:
                label = f"{height}p"
                if has_audio:
                    label += " (video+audio)"
                else:
                    label += " (video only)"
            elif has_audio and not has_video:
                abr = f.get("abr", "")
                label = f"Audio {abr}kbps" if abr else "Audio only"
            else:
                continue

            key = (height, has_video, has_audio, ext)
            if key in seen:
                continue
            seen.add(key)

            format_list.append({
                "format_id": f.get("format_id"),
                "label": label,
                "ext": ext,
                "height": height or 0,
                "filesize": filesize,
                "has_video": has_video,
                "has_audio": has_audio,
            })

        # Sort: highest resolution first
        format_list.sort(key=lambda x: (x["has_video"], x["height"]), reverse=True)

        duration = info.get("duration", 0)
        duration_str = ""
        if duration:
            hours, remainder = divmod(int(duration), 3600)
            minutes, seconds = divmod(remainder, 60)
            if hours:
                duration_str = f"{hours}h {minutes}m {seconds}s"
            else:
                duration_str = f"{minutes}m {seconds}s"

        result = {
            "title": info.get("title", "Unknown"),
            "thumbnail": info.get("thumbnail", ""),
            "channel": info.get("channel", info.get("uploader", "Unknown")),
            "duration": duration_str,
            "view_count": info.get("view_count", 0),
            "upload_date": info.get("upload_date", ""),
            "formats": format_list[:15],  # top 15
        }
        return jsonify(result)

    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/download", methods=["POST"])
def start_download():
    """Start downloading a video. Returns a task_id for progress tracking."""
    data = request.get_json()
    url = data.get("url", "").strip()
    quality = data.get("quality", "best")
    use_cookies = data.get("useCookies", True)

    if not url:
        return jsonify({"error": "URL is required"}), 400

    task_id = str(uuid.uuid4())
    download_tasks[task_id] = {
        "status": "starting",
        "progress": 0,
        "speed": "",
        "eta": "",
        "filename": "",
        "filesize": "",
        "downloaded": "",
        "error": None,
        "phase": "Preparing...",
    }

    def progress_hook(d):
        task = download_tasks[task_id]
        if d["status"] == "downloading":
            task["status"] = "downloading"
            task["phase"] = "Downloading..."

            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes", 0)

            if total > 0:
                task["progress"] = round((downloaded / total) * 100, 1)
            else:
                # Use fragment index if available
                frag_idx = d.get("fragment_index")
                frag_count = d.get("fragment_count")
                if frag_idx and frag_count:
                    task["progress"] = round((frag_idx / frag_count) * 100, 1)

            speed = d.get("speed")
            if speed:
                if speed > 1_000_000:
                    task["speed"] = f"{speed / 1_000_000:.1f} MB/s"
                elif speed > 1_000:
                    task["speed"] = f"{speed / 1_000:.0f} KB/s"
                else:
                    task["speed"] = f"{speed:.0f} B/s"

            eta = d.get("eta")
            if eta is not None:
                mins, secs = divmod(int(eta), 60)
                task["eta"] = f"{mins}m {secs}s" if mins else f"{secs}s"

            if total > 0:
                task["filesize"] = f"{total / (1024**2):.1f} MB" if total < 1024**3 else f"{total / (1024**3):.2f} GB"

            task["downloaded"] = f"{downloaded / (1024**2):.1f} MB" if downloaded < 1024**3 else f"{downloaded / (1024**3):.2f} GB"

        elif d["status"] == "finished":
            task["status"] = "processing"
            task["progress"] = 100
            task["phase"] = "Processing & merging..."
            task["filename"] = d.get("filename", "")

    def do_download():
        task = download_tasks[task_id]
        try:
            # Build format selection
            if quality == "audio":
                format_sel = "bestaudio[ext=m4a]/bestaudio/best"
                merge_ext = "m4a"
            elif quality == "best":
                format_sel = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"
                merge_ext = "mp4"
            elif quality == "720":
                format_sel = "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=720]+bestaudio/best[height<=720]/best"
                merge_ext = "mp4"
            elif quality == "480":
                format_sel = "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=480]+bestaudio/best[height<=480]/best"
                merge_ext = "mp4"
            elif quality == "360":
                format_sel = "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=360]+bestaudio/best[height<=360]/best"
                merge_ext = "mp4"
            else:
                format_sel = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best"
                merge_ext = "mp4"

            ydl_opts = {
                "format": format_sel,
                "merge_output_format": merge_ext,
                "outtmpl": str(DOWNLOAD_DIR / "%(title)s.%(ext)s"),
                "progress_hooks": [progress_hook],
                "quiet": True,
                "no_warnings": True,
                "noprogress": False,
                "extractor_args": {"youtube": {"player_client": ["ios", "mweb"]}},
                "http_headers": {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"},
            }
            # Use uploaded cookies file if available, else browser cookies if toggled
            if COOKIES_FILE.exists():
                ydl_opts["cookiefile"] = str(COOKIES_FILE)
            elif use_cookies:
                ydl_opts["cookiesfrombrowser"] = ("chrome",)

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info)
                # Handle merged output format
                base, _ = os.path.splitext(filename)
                final_file = f"{base}.{merge_ext}"
                if os.path.exists(final_file):
                    task["filename"] = final_file
                elif os.path.exists(filename):
                    task["filename"] = filename
                else:
                    # Try to find the file
                    for f in DOWNLOAD_DIR.iterdir():
                        if f.stem == Path(filename).stem:
                            task["filename"] = str(f)
                            break

            task["status"] = "done"
            task["progress"] = 100
            task["phase"] = "Complete!"

        except Exception as e:
            task["status"] = "error"
            task["error"] = str(e)
            task["phase"] = "Error"

    thread = threading.Thread(target=do_download, daemon=True)
    thread.start()

    return jsonify({"task_id": task_id})


@app.route("/api/progress/<task_id>")
def progress_stream(task_id):
    """SSE endpoint for real-time download progress."""
    def generate():
        while True:
            task = download_tasks.get(task_id)
            if not task:
                data = json.dumps({"error": "Task not found"})
                yield f"data: {data}\n\n"
                break

            data = json.dumps(task)
            yield f"data: {data}\n\n"

            if task["status"] in ("done", "error"):
                break

            time.sleep(0.5)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/file/<task_id>")
def download_file(task_id):
    """Serve the downloaded file to the user's browser."""
    task = download_tasks.get(task_id)
    if not task or task["status"] != "done":
        return jsonify({"error": "File not ready"}), 404

    filepath = task.get("filename", "")
    if not filepath or not os.path.exists(filepath):
        return jsonify({"error": "File not found on disk"}), 404

    return send_file(filepath, as_attachment=True)


if __name__ == "__main__":
    print("\n  >>  YT Downloader running at  http://localhost:5000\n")
    app.run(debug=True, port=5000, threaded=True)
