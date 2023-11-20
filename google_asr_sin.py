import datetime
import re
from typing import Any, List, Dict, Tuple, Union

from dataclasses import dataclass
from scipy.io import wavfile
import numpy as np
import matplotlib.pyplot as plt
import os

import fsspec

from google.cloud import speech_v2

from google.api_core.client_options import ClientOptions
from google.cloud.speech_v2 import SpeechClient
from google.cloud.speech_v2.types import cloud_speech

from google.cloud.speech_v1.types.cloud_speech import RecognizeResponse
# Supported Languages: 
# https://cloud.google.com/speech-to-text/v2/docs/speech-to-text-supported-languages

class RecognitionEngine(object):
  """A class that provides a nice interface to Google's Cloud
  text-to-speech API.
  """
  def __init__(self):
    self._client = None
    self._parent = None

  def CreateSpeechClient(self,
                         gcp_project,
                         model='default_long',
                         ):
    """Acquires the appropriate authentication and creates a Cloud Speech stub.

    The model name is needed because we connect to a different server if the
    model is 'chirp'.

    Returns:
      a Cloud Speech stub.
    """
    self._model = model
    self._project = gcp_project
    self._spoken_punct = False
    self._auto_punct = False


    if model == 'chirp':
      chirp_endpoint = 'us-central1-speech.googleapis.com'
      client_options = ClientOptions(api_endpoint=chirp_endpoint)
      self._location = 'us-central1'
    else:
      client_options = ClientOptions()
      self._location = 'global'

    self._client = SpeechClient(client_options=client_options)

  def ListModels(self, gcp_project: str):
    if self._client is None:
      self.CreateSpeechClient(gcp_project)
    parent = f'projects/{self._project}/locations/{self._location}'
    request = speech_v2.ListRecognizersRequest(parent=parent)
    return self._client.ListModels(request)

  def ListRecognizers(self, gcp_project: str):
    if self._client is None:
      self.CreateSpeechClient(gcp_project)
    parent = f'projects/{self._project}/locations/{self._location}'
    request = speech_v2.ListRecognizersRequest(parent=parent)
    # print(f'ListRecognizers request is: {request}')
    return self._client.list_recognizers(request)

  def CreateRecognizer(self,
                       with_timings=False,
                       locale: str = 'en-US',
                       # gcp_project: str,
                       # recognizer_id: str,
                       # debug=False
                       ):
    # https://cloud.google.com/speech-to-text/v2/docs/medical-models
    if self._model == 'medical_conversation':
      self._spoken_punct = False
      self._auto_punct = True
    elif self._model == 'medical_dictation':
      self._spoken_punct = True
      self._auto_punct = True
    else:
      self._spoken_punct = False
      self._auto_punct = False

    self._recognizer_config = cloud_speech.RecognitionConfig(
        auto_decoding_config=cloud_speech.AutoDetectDecodingConfig(),
        language_codes=[locale],
        model=self._model,
        features = speech_v2.RecognitionFeatures(
                  enable_word_time_offsets = with_timings,
                  enable_automatic_punctuation = self._auto_punct,
                  enable_spoken_punctuation = self._spoken_punct,
              ),
    )

  def RecognizeFile(self, 
                    audio_file_path: str, 
                    with_timings=False, 
                    debug=False) -> cloud_speech.RecognizeResponse:
    """Recognize the speech from a file.
    Returns: https://cloud.google.com/python/docs/reference/speech/latest/google.cloud.speech_v1.types.RecognizeResponse
    
    Note: Unless the file ends in .wav, the file is read in, and the entire
    contents, including the binary header, are passed to the recognizer as a
    16kHz audio waveform."""
    if audio_file_path.endswith('.wav'):
      with fsspec.open(audio_file_path, 'rb') as fp:
        audio_fs, audio_data = wavfile.read(fp)
        return self.RecognizeWaveform(audio_data, audio_fs,
                                      with_timings=with_timings)

    recognizer_name = f'projects/{self._project}/locations/{self._location}/recognizers/_'
    # Create the request we'd like to send
    request = cloud_speech.RecognizeRequest(
        recognizer = recognizer_name,
        config = self._recognizer_config,
        content = self.ReadAudioFile(audio_file_path)
    )
    # Send the request
    if debug:
      print(request)
    response = self._client.recognize(request)
    return response

  def RecognizeWaveform(self,
                        waveform: Union[bytes, np.ndarray],
                        sample_rate: int = 16000,
                        with_timings=False,
                        debug=False) -> RecognizeResponse:
    """Recognize the speech from a waveform."""
    if isinstance(waveform, np.ndarray):
      waveform = waveform.astype(np.int16).tobytes()

    recognizer_name = f'projects/{self._project}/locations/{self._location}/recognizers/_'
    # Create the request we'd like to send
    self._recognizer_config = cloud_speech.RecognitionConfig(
        explicit_decoding_config = cloud_speech.ExplicitDecodingConfig(
            # Change these based on the encoding of the audio
            # See the encoding documentation on how to do this.
            # https://cloud.google.com/speech-to-text/v2/docs/encoding
            encoding = 'LINEAR16',
            sample_rate_hertz = sample_rate,
            audio_channel_count = 1,
        ),
        # auto_decoding_config=cloud_speech.AutoDetectDecodingConfig(),
        language_codes=['en-US'],
        model=self._model,
        features = speech_v2.RecognitionFeatures(
                  enable_word_time_offsets = with_timings,
                  enable_automatic_punctuation = self._auto_punct,
                  enable_spoken_punctuation = self._spoken_punct,
              ),
    )

    request = cloud_speech.RecognizeRequest(
        recognizer = recognizer_name,
        config = self._recognizer_config,
        content = waveform
    )
    if debug:
      print(request)
    # Send the request

    response = self._client.recognize(request)
    return response

  def ReadAudioFile(self, audio_file_path: str):
    # if audio_file_path[0] != '/':
    #   PREFIX = '/google_src/files/head/depot/'
    #   audio_file_path = os.path.join(PREFIX, audio_file_path)
    with fsspec.open(audio_file_path, 'rb') as audio_file:
      audio_data = audio_file.read()
    return audio_data


@dataclass
class RecogResult:
  word: str
  start_time: float
  end_time: float


def parse_time(time_proto) -> float:
  # print(f'Time_proto is a {type(time_proto)}')
  # return time_proto.seconds + time_proto.nanos/1e9
  return time_proto.total_seconds()

def parse_transcript(response: cloud_speech.RecognizeResponse) -> List[RecogResult]:
  """Parse the results from the Cloud ASR engine and return a simple list
  of words and times.  This is for the entire (60s) utterance."""
  words = []
  for a_result in response.results:
    try:
      # For reasons I don't understand sometimes a results is missing the alternatives
      ok = len(a_result.alternatives) > 0
    except:
      ok = False
    # print(ok)
    if not ok:
      continue
    for word in a_result.alternatives[0].words:
      # print(f'Processing: {word}')
      start_time = parse_time(word.start_offset)
      end_time = parse_time(word.end_offset)
      recog_result = RecogResult(word.word.lower(), start_time, end_time)
      words.append(recog_result)
    words.append(RecogResult('.', end_time, end_time))
    # print(words[-1])
  return words

def print_all_sentences(results):
  for r in results:
    if r.alternatives:
      print(r.alternatives[0].transcript)
    else:
      print('No alternatives')
      

def generate_ffmpeg_cmds():
  """Generate the FFMPEG commands to downsample and rename the QuickSIN files.
  The Google drive data from Matt has these files:
*   34 Sep List 11.aif - Stereo utterances: clean sentences on the left, constant amplitude babble noise on the right
*   34 Sep List 11_sentence.wav - Mono clean sentences
*   34 Sep List 11_babble.wav - Mono babble
*   List 11.aif - Mono mixed test sentences, with the SNR stepping down after each sentence.

  """
  for i in range(1, 13):
    input = f'{23+i} Sep List {i}_sentence.wav'
    output = f'QuickSIN22/Clean List {i}.wav'
    print(f'ffmpeg -i "{input}" -ar 22050 "{output}"')

    input = f'List {i}.aif'
    output = f'QuickSIN22/Babble List {i}.wav'
    print(f'ffmpeg -i "{input}" -ar 22050 "{output}"')

    print()



SpinFileTranscripts = Dict[str, cloud_speech.RecognizeResponse]

def recognize_all_spin(all_wavs: List[str],
                       asr_engine: RecognitionEngine,
                       debug=False) -> SpinFileTranscripts:
  """Recognize some SPiN sentences using the specified ASR engine.
  return the raw Cloud ASR responses in a dictionary keyed by the
  (last part of) the filename."""
  all_results = {}
  for f in all_wavs:
    if 'Calibration' in f:
      continue
    pretty_file = os.path.basename(f)
    if debug:
      print('Recognizing', pretty_file)
    resp = asr_engine.RecognizeFile(f, with_timings=True, debug=debug)
    if debug:
      print(f'{pretty_file}:',)
      for result in resp.results:
        if result.alternatives:
          print(f'   {result.alternatives[0].transcript}')
        else:
          print('.   ** Empty ASR Result **')
    all_results[pretty_file] = resp
  return all_results
# These are the structures returned by the Cloud Spech-to-Text API



#################### SPIN TESTS ############################

# Pages 111 and 112 of this PDF: https://etda.libraries.psu.edu/files/final_submissions/5788

key_word_list = """
L 0 S 0  white silk jacket any shoes
L 0 S 1  child crawled into dense grass
L 0 S 2  Footprints show/showed path took beach
L 0 S 3  event near edge fresh air
L 0 S 4  band Steel 3/three inches/in wide
L 0 S 5  weight package seen high scale

L 1 S 0  tear/Tara thin sheet yellow pad
L 1 S 1  cruise Waters Sleek yacht fun
L 1 S 2  streak color down left Edge
L 1 S 3  done before boy/boys see it
L 1 S 4  Crouch before jump miss mark
L 1 S 5  square peg settle round hole

L 2 S 0  pitch straw through door stable
L 2 S 1  sink thing which pile/piled dishes
L 2 S 2  post no bills office wall
L 2 S 3  dimes showered/shower down all sides
L 2 S 4  pick card slip under pack/Pact
L 2 S 5  store jammed before sale start

L 3 S 0  sense smell better than touch
L 3 S 1  picked up dice second roll
L 3 S 2  drop ashes worn/Warren Old rug
L 3 S 3  couch cover Hall drapes blue
L 3 S 4  stems Tall Glasses cracked broke
L 3 S 5  cleats sank/sink deeply soft turf

L 4 S 0  have better than wait Hope
L 4 S 1  screen before fire kept Sparks
L 4 S 2  thick glasses helped/help read print/prints
L 4 S 3  chair looked strong no bottom
L 4 S 4  told wild Tales/tails frighten him
L 4 S 5  force equal would move Earth

L 5 S 0  leaf drifts along slow spin
L 5 S 1  pencil cut sharp both ends
L 5 S 2  down road way grain farmer
L 5 S 3  best method fix place clips
L 5 S 4  if Mumble your speech lost
L 5 S 5  toad Frog hard tell apart

L 6 S 0  kite dipped swayed/suede stayed aloft/loft
L 6 S 1  beatle/beetle drowned hot June/Tunes sun/son
L 6 S 2  theft Pearl pin Kept Secret
L 6 S 3  wide grin earned many friends
L 6 S 4  hurdle pit aid long Pole
L 6 S 5  Peep/keep under tent see Clown

L 7 S 0  sun came light Eastern sky
L 7 S 1  stale smell old beer lingers
L 7 S 2  desk firm on shaky floor
L 7 S 3  list names carved around base
L 7 S 4  news struct/struck out Restless Minds
L 7 S 5  Sand drifts/Drift over sill/sale house

L 8 S 0  take shelter tent keep still
L 8 S 1  Little Tales/tails they tell false
L 8 S 2  press pedal with left foot
L 8 S 3  black trunk fell from Landing/landings
L 8 S 4  cheap clothes flashy/flash don't last
L 8 S 5  night alarm roused/roust deep sleep

L 9 S 0  dots light betray/betrayed black cat
L 9 S 1  put chart mantle Tack down
L 9 S 2  steady drip worse drenching rain
L 9 S 3  flat pack less luggage space
L 9 S 4  gloss/glass top made unfit read
L 9 S 5  Seven Seals stamped great sheets

L10 S 0  marsh freeze when cold enough
L10 S 1  gray mare walked before colt/cold
L10 S 2  bottles hold four/for kinds rum
L10 S 3  wheeled/wheled bike past winding road
L10 S 4  throw used paper cup plate
L10 S 5  wall phone ring loud often

L11 S 0  hinge door creaked old age
L11 S 1  bright lanterns Gay dark lawn
L11 S 2  offered proof  form large chart
L11 S 3  their eyelids droop/drop want sleep
L11 S 4  many ways do these things
L11 S 5  we like see clear weather
""".split('\n')

def word_alternatives(words) -> List[str]:
  """Convert a string with words separated by '/' into a tuple."""
  if '/' in words:
    return words.split('/')
  return [words,]


def ingest_spin_keyword_lists(key_word_list) -> Dict[Tuple[int, int], 
                                                     List[List[str]]]:
  """Convert the text from the big string above into a list of key words 
  (and alternatives) that describe the expected answers from a SPIN test."""
  all_keyword_dict = {}
  for line in key_word_list:
    line = line.strip().lower()
    if not line: continue
    list_number = int(line[1:3])
    sentence_number = int(line[5:7])
    key_words = line[7:].split(' ')
    key_words = [w for w in key_words if w]
    key_list = [word_alternatives(w) for w in key_words]
    if len(key_list) != 5:
      print(f'Have too many words in L{list_number} S{sentence_number}:',
            key_list)
    all_keyword_dict[list_number, sentence_number] = key_list
  return all_keyword_dict

all_keyword_dict = ingest_spin_keyword_lists(key_word_list)
