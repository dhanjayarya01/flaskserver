from flask import Flask, request, jsonify, send_file, Response
from flask_cors import CORS
import yt_dlp
import os
import logging
import json
import threading
from queue import Queue
import datetime
import signal
import psutil
import tempfile
import io
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api.formatters import TextFormatter
import google.generativeai as genai
import sys

app = Flask(__name__)
CORS(app)

# Get the base path for the executable/script
if getattr(sys, 'frozen', False):
    # Running as compiled executable
    BASE_PATH = sys._MEIPASS
else:
    # Running as script
    BASE_PATH = os.path.dirname(os.path.abspath(__file__))

# Update FFMPEG path to use bundled binary
FFMPEG_PATH = os.path.join(BASE_PATH, 'bin', 'ffmpeg.exe')
FFPROBE_PATH = os.path.join(BASE_PATH, 'bin', 'ffprobe.exe')

# Configure yt-dlp to use the bundled ffmpeg
ydl_opts = {
    'ffmpeg_location': os.path.dirname(FFMPEG_PATH),
}

# Add a function to get or create model with user's API key
def get_gemini_model(api_key):
    if not api_key:
        raise ValueError("No API key provided")
    
    genai.configure(api_key=api_key)
    return genai.GenerativeModel('gemini-pro')

class DownloadProgress:
    def __init__(self):
        self.progress = 0
        self.speed = "0 KiB/s"
        self.eta = "00:00"
        self.status = "starting"
        self.process = None

download_progress = DownloadProgress()
current_process = None

def progress_hook(d):
    if d['status'] == 'downloading':
        # Calculate progress percentage
        total_bytes = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
        if total_bytes > 0:
            downloaded = d.get('downloaded_bytes', 0)
            download_progress.progress = (downloaded / total_bytes) * 100
            download_progress.speed = d.get('speed', 0)
            download_progress.eta = d.get('eta', 0)
    elif d['status'] == 'finished':
        download_progress.progress = 100
        download_progress.status = 'finished'

@app.route('/download')
def download_video():
    temp_dir = None
    try:
        video_id = request.args.get('videoId')
        quality = request.args.get('formatId')
        
        app.logger.info(f"Download request - Video ID: {video_id}, Quality: {quality}")
        
        url = f'https://www.youtube.com/watch?v={video_id}'
        
        # Create temporary directory
        temp_dir = tempfile.mkdtemp()
        output_template = os.path.join(temp_dir, '%(title)s.%(ext)s')

        # Check if this is an audio download
        is_audio = quality.startswith('audio_')
        if is_audio:
            format_str = quality.replace('audio_', '')  # Get the actual format ID
            ydl_opts = {
                'format': format_str,
                'outtmpl': output_template,
                'progress_hooks': [progress_hook],
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
                'ffmpeg_location': FFMPEG_PATH,
                'keepvideo': False,
                'quiet': False,
                'verbose': True
            }
        else:
            # Existing video format options
            format_map = {
                '4k': 'bestvideo[height<=2160][ext=webm]+bestaudio[ext=webm]/best[height<=2160]',
                '1440p': 'bestvideo[height<=1440][ext=webm]+bestaudio[ext=webm]/best[height<=1440]',
                '1080p': 'bestvideo[height<=1080][ext=webm]+bestaudio[ext=webm]/best[height<=1080]',
                '720p': 'bestvideo[height=720][ext=webm]+bestaudio[ext=webm]/best[height=720]',
                '480p': 'bestvideo[height=480][ext=webm]+bestaudio[ext=webm]/best[height=480]'
            }
            format_str = format_map.get(quality, format_map['1080p'])
            ydl_opts = {
                'format': format_str,
                'outtmpl': output_template,
                'merge_output_format': 'mkv',
                'prefer_ffmpeg': True,
                'ffmpeg_location': FFMPEG_PATH,
                'progress_hooks': [progress_hook],
                'postprocessors': [{
                    'key': 'FFmpegVideoRemuxer',
                    'preferedformat': 'mkv',
                }],
                'ffmpeg_args': [
                    '-c', 'copy',
                    '-strict', 'experimental'
                ],
                'quiet': False,
                'verbose': True
            }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                download_progress.process = psutil.Process()
                info = ydl.extract_info(url, download=True)
                
                # For audio downloads, look for the MP3 file
                if is_audio:
                    filename = os.path.splitext(ydl.prepare_filename(info))[0] + '.mp3'
                else:
                    filename = ydl.prepare_filename(info)
                
                if not os.path.exists(filename):
                    raise Exception(f"Downloaded file not found: {filename}")
                
                app.logger.info(f"Download completed: {filename}")
                
                # Read file into memory before sending
                with open(filename, 'rb') as f:
                    file_data = f.read()
                
                # Clean up the file immediately after reading
                try:
                    os.remove(filename)
                except:
                    pass
                
                # Set correct mimetype based on file type
                mimetype = 'audio/mp3' if is_audio else 'video/mp4'
                
                return send_file(
                    io.BytesIO(file_data),
                    as_attachment=True,
                    download_name=os.path.basename(filename),
                    mimetype=mimetype
                )
                
            except Exception as e:
                app.logger.error(f"Error during download: {str(e)}")
                raise
            finally:
                download_progress.process = None
                
    except Exception as e:
        download_progress.process = None
        app.logger.error(f"Download error: {str(e)}", exc_info=True)
        return jsonify({
            'error': str(e),
            'details': 'Check server logs for more information'
        }), 500
    finally:
        # Clean up temporary directory if it exists
        if temp_dir and os.path.exists(temp_dir):
            try:
                import shutil
                shutil.rmtree(temp_dir, ignore_errors=True)
            except:
                pass

@app.route('/formats')
def get_formats():
    video_id = request.args.get('videoId')
    
    if not video_id:
        return jsonify({'error': 'No video ID provided'}), 400
        
    url = f'https://www.youtube.com/watch?v={video_id}'
    
    try:
        with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
            info = ydl.extract_info(url, download=False)
            formats = info.get('formats', [])
            
            # Get available heights for all video formats
            format_info = {}
            audio_formats = []

            # Process formats to separate video and audio
            for f in formats:
                # Video formats
                if f.get('vcodec', 'none') != 'none' and f.get('acodec', 'none') == 'none':
                    height = f.get('height', 0)
                    if height > 0:
                        if height not in format_info or f.get('filesize', 0) > format_info[height]['filesize']:
                            format_info[height] = {
                                'format_id': f.get('format_id'),
                                'ext': f.get('ext'),
                                'vcodec': f.get('vcodec'),
                                'filesize': f.get('filesize', 0)
                            }
                
                # Audio formats - only include downloadable formats
                elif f.get('vcodec', 'none') == 'none' and f.get('acodec', 'none') != 'none':
                    # Skip formats without audio bitrate or with protocol that can't be downloaded
                    if f.get('abr') and not any(p in f.get('protocol', '') for p in ['dash', 'http_dash_segments']):
                        audio_formats.append({
                            'format_id': f.get('format_id'),
                            'ext': f.get('ext'),
                            'filesize': f.get('filesize', 0),
                            'abr': f.get('abr', 0),  # audio bitrate
                            'acodec': f.get('acodec', 'unknown')
                        })

            format_options = []
            
            # Add video formats (existing code)
            resolution_map = {
                2160: {'id': '4k', 'label': '4K (2160p)'},
                1440: {'id': '1440p', 'label': 'QHD (1440p)'},
                1080: {'id': '1080p', 'label': 'Full HD (1080p)'},
                720: {'id': '720p', 'label': 'HD (720p)'},
                480: {'id': '480p', 'label': 'SD (480p)'}
            }
            
            # Add video formats
            for height in sorted(format_info.keys(), reverse=True):
                if height in resolution_map:
                    format_options.append({
                        'formatId': resolution_map[height]['id'],
                        'extension': 'mp4',
                        'quality': f'{height}p',
                        'label': resolution_map[height]['label'],
                        'vcodec': format_info[height]['vcodec'],
                        'type': 'video'
                    })
            
            # Define audio quality levels
            audio_quality_levels = {
                'high': {'min_abr': 256, 'label': 'High Quality'},
                'medium': {'min_abr': 128, 'label': 'Medium Quality'},
                'low': {'min_abr': 0, 'label': 'Low Quality'}
            }

            # Sort audio formats by bitrate
            audio_formats.sort(key=lambda x: x['abr'], reverse=True)
            
            # Group audio formats by quality level
            added_qualities = set()  # Track added quality levels
            for audio in audio_formats:
                abr = audio['abr']
                # Determine quality level
                if abr >= 256 and 'high' not in added_qualities:
                    quality_label = 'High Quality'
                    quality_key = 'high'
                elif abr >= 128 and 'medium' not in added_qualities:
                    quality_label = 'Medium Quality'
                    quality_key = 'medium'
                elif 'low' not in added_qualities:
                    quality_label = 'Low Quality'
                    quality_key = 'low'
                else:
                    continue  # Skip if we already have this quality level

                added_qualities.add(quality_key)
                format_options.append({
                    'formatId': f"audio_{audio['format_id']}",
                    'extension': 'mp3',
                    'quality': f"{int(abr)}kbps",
                    'label': f"Audio - {quality_label} ({int(abr)}kbps)",
                    'type': 'audio',
                    'codec': audio['acodec']
                })
            
            return jsonify(format_options)
            
    except Exception as e:
        app.logger.error(f"Error getting formats: {str(e)}")
        return jsonify({'error': str(e)}), 400

@app.route('/progress')
def get_progress():
    return jsonify({
        'progress': download_progress.progress,
        'speed': download_progress.speed,
        'eta': download_progress.eta,
        'status': download_progress.status
    })

@app.route('/cancel')
def cancel_download():
    try:
        if download_progress.process:
            # Kill the process and its children
            parent = psutil.Process(download_progress.process.pid)
            for child in parent.children(recursive=True):
                child.kill()
            parent.kill()
            
            # Reset progress
            download_progress.process = None
            download_progress.progress = 0
            download_progress.status = "cancelled"
            
            app.logger.info("Download cancelled successfully")
            return jsonify({'status': 'cancelled'})
    except Exception as e:
        app.logger.error(f"Error cancelling download: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/playlist-info')
def get_playlist_info():
    try:
        playlist_id = request.args.get('playlistId')
        if not playlist_id:
            return jsonify({'error': 'No playlist ID provided'}), 400
            
        url = f'https://www.youtube.com/playlist?list={playlist_id}'
        
        with yt_dlp.YoutubeDL({'quiet': True, 'extract_flat': True}) as ydl:
            info = ydl.extract_info(url, download=False)
            
            videos = [{
                'id': entry['id'],
                'title': entry['title'],
                'duration': entry.get('duration', 0)
            } for entry in info['entries']]
            
            return jsonify({
                'title': info.get('title', 'Playlist'),
                'videos': videos
            })
            
    except Exception as e:
        app.logger.error(f"Error getting playlist info: {str(e)}")
        return jsonify({'error': str(e)}), 400

@app.route('/reset-progress')
def reset_progress():
    download_progress.progress = 0
    download_progress.speed = 0
    download_progress.eta = 0
    download_progress.status = "starting"
    return jsonify({'status': 'reset'})

@app.route('/get-transcript-languages')
def get_transcript_languages():
    try:
        video_id = request.args.get('videoId')
        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
        
        languages = []
        for transcript in transcript_list:
            languages.append({
                'code': transcript.language_code,
                'name': transcript.language,
                'isGenerated': transcript.is_generated
            })
            
        return jsonify(languages)
    except Exception as e:
        return jsonify({'error': str(e)}), 400

@app.route('/check-transcript')
def check_transcript():
    try:
        video_id = request.args.get('videoId')
        if not video_id:
            return jsonify({'hasTranscript': False, 'error': 'No video ID provided'})
            
        # Try to get available transcripts
        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
        return jsonify({'hasTranscript': True})
    except Exception as e:
        logging.error(f"Error checking transcript: {str(e)}")
        return jsonify({'hasTranscript': False, 'error': str(e)})

@app.route('/get-transcript')
def get_transcript():
    try:
        video_id = request.args.get('videoId')
        language = request.args.get('language', 'en')  # Default to English
        
        if not video_id:
            return jsonify({'error': 'No video ID provided'}), 400
            
        # Get transcript list
        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
        
        try:
            # Try to get manual transcript in requested language
            transcript = transcript_list.find_transcript([language])
        except:
            try:
                # Try to get auto-generated transcript
                transcript = transcript_list.find_generated_transcript([language])
            except:
                # If no transcript in requested language, get first available and translate
                transcript = transcript_list.find_generated_transcript(['hi'])  # Try Hindi auto-generated
                if language != 'hi':
                    transcript = transcript.translate(language)
        
        # Get the actual transcript data
        transcript_data = transcript.fetch()
        
        # Format transcript with timestamps
        formatted_transcript = []
        for entry in transcript_data:
            time = int(entry['start'])
            minutes = time // 60
            seconds = time % 60
            timestamp = f"[{minutes:02d}:{seconds:02d}] "
            formatted_transcript.append(f"{timestamp}{entry['text']}")
            
        return jsonify({
            'transcript': '\n'.join(formatted_transcript),
            'language': transcript.language,
            'isGenerated': transcript.is_generated
        })
        
    except Exception as e:
        logging.error(f"Error getting transcript: {str(e)}")
        return jsonify({'error': str(e)}), 400

@app.route('/summarize', methods=['POST'])
def summarize_text():
    try:
        data = request.json
        text = data.get('text', '')
        api_key = request.headers.get('X-Gemini-Key')

        if not text:
            return jsonify({'error': 'No text provided'}), 400
        
        if not api_key:
            return jsonify({'error': 'No API key provided'}), 401

        # Create prompt for better summarization
        prompt = """
        Please provide a concise summary of this video transcript. Focus on:
        - Main topics and key points
        - Important details and conclusions
        - Keep the summary clear and well-structured
        
        Transcript:
        """

        try:
            # Get model with user's API key
            model = get_gemini_model(api_key)
            response = model.generate_content(f"{prompt}\n{text}")
            
            if response.prompt_feedback.block_reason:
                raise Exception(f"Content blocked: {response.prompt_feedback.block_reason}")
                
            summary = response.text
            return jsonify({'summary': summary})
            
        except Exception as gemini_error:
            error_msg = str(gemini_error)
            if "API key not valid" in error_msg.lower():
                return jsonify({
                    'error': 'Invalid API key. Please check your API key and try again.',
                    'details': error_msg
                }), 401
            
            logging.error(f"Gemini API error: {error_msg}")
            return jsonify({
                'error': 'Failed to generate summary. Please try again later.',
                'details': error_msg
            }), 500

    except Exception as e:
        logging.error(f"Error summarizing text: {str(e)}")
        return jsonify({'error': str(e)}), 400

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    app.logger.info("Starting server...")
    app.run(debug=True, threaded=True) 