import yt_dlp
import json
import tqdm
import os
import shutil
from os import listdir
from os.path import isfile, join
from pathlib import Path
import json, requests
import math
from dateutil import parser

s3 = None
s3_root_path = None
s3_output_folder = None

def set_global_variables(input_s3,input_s3_root_path,input_s3_output_folder):
  global s3
  global s3_root_path
  global s3_output_folder
  s3 = input_s3
  s3_root_path = input_s3_root_path
  s3_output_folder = input_s3_output_folder


def get_playlist_metadata(playlist_url: str):
  ydl_opts = {
      'ignoreerrors': True,
      'extract_flat': True, 
      'skip_download': True,
      'quiet': True
  }

  with yt_dlp.YoutubeDL(ydl_opts) as ydl:
      info = ydl.extract_info(playlist_url, download=False)
  
  return info

# otehr comment

def get_playlist_items(playlist_id: str, api_key: str , min_published_date=None,max_published_date=None):
  URL1 = 'https://www.googleapis.com/youtube/v3/playlistItems?part=contentDetails,snippet&maxResults=50&fields=items/contentDetails/videoId,nextPageToken,items/snippet/publishedAt,&key={}&playlistId={}&pageToken='.format(api_key, playlist_id)

  next_page = ''
  vid_list = [] 

  while True:
      results = json.loads(requests.get(URL1 + next_page).text)
      
      for x in results['items']:
          publish_date = parser.isoparse(x['snippet']['publishedAt'])
          is_valid_date = True
          if max_published_date is not None:
              is_valid_date = publish_date <= max_published_date
          if min_published_date is not None:
              is_valid_date = publish_date >= min_published_date
          if is_valid_date:
              vid_list.append('https://www.youtube.com/watch?v=' + x['contentDetails']['videoId'])

      if 'nextPageToken' in results:
          next_page = results['nextPageToken']
      else:
          break

  return vid_list


def process_downloaded_audio(audioFile: str, s3,s3_root_path,s3_output_folder):
    # split file into smaller chunks
    print("  Splitting voice sections ...", end='')
    os.system("python split_segments.py --file_path "+ audioFile +" --cleanup_vocals_files")
    print("  [done] ")

    # upload splits to S3
    audio_file_name = Path(audioFile).stem
    parent_folder = Path(audioFile).parent

    print("  Uploading to S3 ...", end='')

    split_output_folder = join(parent_folder, 'output-8',audio_file_name,"chunks-withMin")
    split_files = [join(split_output_folder, f) for f in listdir(split_output_folder) if (isfile(join(split_output_folder, f)) and f.endswith(".mp3"))]
    for split in split_files:
      split_file_name = os.path.basename(split)
      s3.put(split, f"s3://{s3_root_path}/{s3_output_folder}/{audio_file_name}_{split_file_name}")
    
    print("  [done] ")

    # delete temp files
    print("  Deleting temp files ...", end='')
    os.remove(audioFile)
    shutil.rmtree(join(parent_folder, 'output-8',audio_file_name))
    print("  [done] ")


def download_progress_hook(d):
  if d['status'] == 'finished':
      filename=d['filename']
      print(" "+filename)
      process_downloaded_audio(os.path.abspath(d['filename']), s3,s3_root_path,s3_output_folder)

def read_progress_lines_from_all_slots(playlistTitle,playlistId,num_slots):
  print("Reading progress tracking info from all slots")
  processed_lines =[]
  progress_file_names = []
  progress_file_names.append(f"progress_{playlistTitle}_{playlistId}.log")
    
  for other_slots in range(1, num_slots):
    currSlotName =  f"progress_{playlistTitle}_{playlistId}_{other_slots}.log"
    progress_file_names.append(currSlotName)

  for progress_tracker_other in progress_file_names:
    # files could have been downloaded on other slots during previous runs. Read all progress files
    progress_tracker_s3_other = f"s3://{s3_root_path}/_progressFiles/{progress_tracker_other}"

    if(s3.exists(progress_tracker_s3_other)):
      s3.download(progress_tracker_s3_other,progress_tracker_other)
      with open(progress_tracker_other) as file:
        processed_lines += [line.rstrip() for line in file]
    else:
      print("progress file not found on S3: "+ progress_tracker_s3_other)

  print(f"Processed video urls across all slots: {len(processed_lines)}")
  return processed_lines

def download_playlist_items(playlistInfo, videoUrls, s3, s3_root_path,num_slots=1, slot_index=0, skip_downloads=False, progress_file_prefix=None):
    playlistTitle = playlistInfo['title'].replace(' ', '_').replace('\'', '_').lower()
    playlistId = playlistInfo['id']

    # dowload options
    downloaded_output_template = f"dataset/{playlistTitle}_%(upload_date>%Y-%m-%d)s_%(id)s.wav"
    ydl_opts = {
        'format': 'bestaudio/best',
        "audio-format": "wav",
        'outtmpl': downloaded_output_template,
        'ignoreerrors': True,
        'no-playlist': True,
        'quiet': True,
        'cookies': '/content/youtube.com_cookies.txt',
        'progress_hooks': [download_progress_hook]
    }
    workingFolder = 'dataset'

    if(num_slots > 1):
      print(f"Splitting input list of {len(videoUrls)} into {num_slots} slots")
      itemsPerSlot = math.ceil(len(videoUrls)/num_slots)
      startIndex = slot_index*itemsPerSlot
      videoUrls = videoUrls[startIndex: min(startIndex+itemsPerSlot, len(videoUrls))]
      print(f"filtered videoUrls has length {len(videoUrls)}")

    #S3 metadata
    prefix = "progress"
    if progress_file_prefix:
       prefix += "_" + progress_file_prefix

    progress_tracker = f"{prefix}_{playlistTitle}_{playlistId}.log"
    if(slot_index > 0):
      progress_tracker = f"{prefix}_{playlistTitle}_{playlistId}_{slot_index}.log"

    progress_tracker_s3 = f"s3://{s3_root_path}/_progressFiles/{progress_tracker}"
    if(s3.exists(progress_tracker_s3)):
        s3.download(progress_tracker_s3,progress_tracker)
    else:
        # create empty file
        with open(progress_tracker, 'w') as fp:
            pass
   
    if(num_slots > 1):
      processed_lines = read_progress_lines_from_all_slots(playlistTitle,playlistId,num_slots)
    else:
      with open(progress_tracker) as file:
          processed_lines = [line.rstrip() for line in file]
  
    for record in tqdm.notebook.tqdm(videoUrls, desc="Downloading"):
          try:
            current_url = record
          except:
            continue
          if (current_url in processed_lines):
            print("skipping "+ current_url)
            continue
          # Debug hook
          if (skip_downloads):
            print("[DEBUG] skipping "+ current_url)
            continue

          with yt_dlp.YoutubeDL(ydl_opts) as ydl:
              error_code = ydl.download(current_url)

          # Append current URl to end of file
          with open(progress_tracker, "a") as file_object:
            file_object.write(current_url)
            file_object.write("\n")
          
          # save file
          s3.put(progress_tracker, progress_tracker_s3)