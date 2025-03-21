from flask import Flask, render_template, request, send_file, flash, redirect, url_for, Response, jsonify
from pydub import AudioSegment
import os
import re
import speech_recognition as sr
import time
from werkzeug.utils import secure_filename
import json
import queue
import threading

app = Flask(__name__)
app.secret_key = "supersecretkey"

# Directories
UPLOAD_FOLDER = "uploads"
CHUNK_FOLDER = "audio_chunks"
TRANSCRIPT_FOLDER = "transcriptions"
for folder in [UPLOAD_FOLDER, CHUNK_FOLDER, TRANSCRIPT_FOLDER]:
    os.makedirs(folder, exist_ok=True)

app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
ALLOWED_EXTENSIONS = {"mp3", "m4a"}

# Helper functions
def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def convert_to_wav(audio_file_path):
    try:
        # Get the file extension
        ext = os.path.splitext(audio_file_path)[1].lower()
        
        # Load audio based on format
        if ext == '.mp3':
            audio = AudioSegment.from_mp3(audio_file_path)
        elif ext == '.m4a':
            audio = AudioSegment.from_file(audio_file_path, format='m4a')
        else:
            raise ValueError(f"Unsupported audio format: {ext}")
            
        wav_file_path = audio_file_path.rsplit('.', 1)[0] + '.wav'
        audio.export(wav_file_path, format="wav")
        print(f"Conversion successful! Saved as: {wav_file_path}")
        return wav_file_path
    except Exception as e:
        print(f"Error converting audio to WAV: {str(e)}")
        raise

def natural_sort_key(file_name):
    return [int(text) if text.isdigit() else text for text in re.split(r'(\d+)', file_name)]

# Global queue for transcription updates
transcription_queues = {}

def process_audio_file(audio_path, queue_id):
    try:
        filename = os.path.basename(audio_path)
        filename_without_ext = os.path.splitext(filename)[0]
        
        # Convert to WAV
        print(f"Converting {filename} to WAV...")
        try:
            wav_file = convert_to_wav(audio_path)
        except Exception as e:
            raise Exception(f"Audio conversion failed: {str(e)}")
        
        # Split WAV into chunks
        print(f"Splitting {wav_file} into chunks...")
        try:
            audio = AudioSegment.from_wav(wav_file)
            chunk_length_ms = 60000  # 60 seconds
            total_length_ms = len(audio)
            num_chunks = total_length_ms // chunk_length_ms + (1 if total_length_ms % chunk_length_ms else 0)
            
            output_dir = os.path.join(CHUNK_FOLDER, f"audio_chunks_for_{filename_without_ext}")
            os.makedirs(output_dir, exist_ok=True)
            
            # Split and save chunks
            chunk_files = []
            for i in range(num_chunks):
                start = i * chunk_length_ms
                end = min((i + 1) * chunk_length_ms, total_length_ms)
                chunk = audio[start:end]
                chunk_filename = os.path.join(output_dir, f"{i + 1}.wav")
                chunk.export(chunk_filename, format="wav")
                chunk_files.append(chunk_filename)
                print(f"Chunk {i + 1}/{num_chunks} created")
        except Exception as e:
            raise Exception(f"Chunk creation failed: {str(e)}")
        
        # Transcribe chunks
        recognizer = sr.Recognizer()
        full_transcription = ""
        
        for index, chunk_file in enumerate(chunk_files, 1):
            try:
                with sr.AudioFile(chunk_file) as source:
                    audio_data = recognizer.record(source)
                try:
                    text = recognizer.recognize_google(audio_data, language="ne-NP")
                except sr.UnknownValueError:
                    text = "{not understood here}"
                    print(f"Chunk {index}/{num_chunks} not understood")
                except sr.RequestError as e:
                    error_msg = f"Could not request results: {str(e)}"
                    print(error_msg)
                    raise Exception(error_msg)
                
                # Add the text (whether recognized or not) to transcription
                full_transcription += text + " "
                
                # Send progress and transcription updates
                if queue_id in transcription_queues:
                    transcription_queues[queue_id].put({
                        'type': 'progress',
                        'chunk': index,
                        'total': num_chunks
                    })
                    transcription_queues[queue_id].put({
                        'type': 'transcription',
                        'text': text + ' '
                    })
                print(f"Chunk {index}/{num_chunks} processed")
                    
            except Exception as e:
                raise Exception(f"Transcription failed at chunk {index}: {str(e)}")
        
        # Save complete transcription
        try:
            transcription_file = os.path.join(
                TRANSCRIPT_FOLDER, 
                f"transcription_for_{filename_without_ext}.txt"
            )
            with open(transcription_file, "w", encoding="utf-8") as f:
                f.write(full_transcription.strip())
            
            # Send completion message
            if queue_id in transcription_queues:
                transcription_queues[queue_id].put({
                    'type': 'complete',
                    'download_link': transcription_file
                })
        except Exception as e:
            raise Exception(f"Failed to save transcription: {str(e)}")
            
    except Exception as e:
        error_msg = f"Processing failed: {str(e)}"
        print(error_msg)
        if queue_id in transcription_queues:
            transcription_queues[queue_id].put({
                'type': 'error',
                'message': error_msg
            })
    finally:
        # Cleanup
        if queue_id in transcription_queues:
            del transcription_queues[queue_id]

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400
    
    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)
        print(f"File saved to {filepath}")  # Debug log
        
        # Create a queue for this upload
        queue_id = filename
        transcription_queues[queue_id] = queue.Queue()
        print(f"Created queue with ID: {queue_id}")  # Debug log
        
        # Start processing in a separate thread
        thread = threading.Thread(target=process_audio_file, args=(filepath, queue_id))
        thread.daemon = True
        thread.start()
        print(f"Started processing thread for {filename}")  # Debug log
        
        return jsonify({'status': 'success', 'queue_id': queue_id})
    
    return jsonify({'error': 'Invalid file type'}), 400

@app.route('/stream')
def stream():
    queue_id = request.args.get('queue_id')
    print(f"Stream requested for queue_id: {queue_id}")  # Debug log
    
    if not queue_id or queue_id not in transcription_queues:
        print(f"Queue not found: {queue_id}")  # Debug log
        return Response('Queue not found', status=404)

    def generate():
        while True:
            try:
                message = transcription_queues[queue_id].get(timeout=60)
                print(f"Sending message: {message}")  # Debug log
                yield f"data: {json.dumps(message)}\n\n"
                if message['type'] in ['complete', 'error']:
                    break
            except queue.Empty:
                print("Queue timeout")  # Debug log
                break
    
    return Response(generate(), mimetype='text/event-stream')

@app.route('/download/<path:filename>')
def download_file(filename):
    return send_file(filename, as_attachment=True)

if __name__ == '__main__':
    app.run(debug=True)

