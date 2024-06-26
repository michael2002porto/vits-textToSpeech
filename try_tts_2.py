from IPython.display import Audio
import os
import re
import glob
import json
import tempfile
import math
import torch
from torch import nn
from torch.nn import functional as F
from torch.utils.data import DataLoader
import numpy as np
import commons
import utils
import argparse
import subprocess
import wave
from data_utils import TextAudioLoader, TextAudioCollate, TextAudioSpeakerLoader, TextAudioSpeakerCollate
from models import SynthesizerTrn
from scipy.io.wavfile import write
import soundfile as sf

def preprocess_char(text, lang=None):
    """
    Special treatement of characters in certain languages
    """
    # print(lang)
    if lang == 'ron':
        text = text.replace("ț", "ţ")
    return text

class TextMapper(object):
    def __init__(self, vocab_file):
        self.symbols = [x.replace("\n", "") for x in open(vocab_file, encoding="utf-8").readlines()]
        self.SPACE_ID = self.symbols.index(" ")
        self._symbol_to_id = {s: i for i, s in enumerate(self.symbols)}
        self._id_to_symbol = {i: s for i, s in enumerate(self.symbols)}

    def text_to_sequence(self, text, cleaner_names):
        '''Converts a string of text to a sequence of IDs corresponding to the symbols in the text.
        Args:
        text: string to convert to a sequence
        cleaner_names: names of the cleaner functions to run the text through
        Returns:
        List of integers corresponding to the symbols in the text
        '''
        sequence = []
        clean_text = text.strip()
        for symbol in clean_text:
            symbol_id = self._symbol_to_id[symbol]
            sequence += [symbol_id]
        return sequence

    def uromanize(self, text, uroman_pl):
        iso = "xxx"
        with tempfile.NamedTemporaryFile() as tf, \
             tempfile.NamedTemporaryFile() as tf2:
            with open(tf.name, "w") as f:
                f.write("\n".join([text]))
            cmd = f"perl " + uroman_pl
            cmd += f" -l {iso} "
            cmd +=  f" < {tf.name} > {tf2.name}"
            os.system(cmd)
            outtexts = []
            with open(tf2.name) as f:
                for line in f:
                    line =  re.sub(r"\s+", " ", line).strip()
                    outtexts.append(line)
            outtext = outtexts[0]
        return outtext

    def get_text(self, text, hps):
        text_norm = self.text_to_sequence(text, hps.data.text_cleaners)
        if hps.data.add_blank:
            text_norm = commons.intersperse(text_norm, 0)
        text_norm = torch.LongTensor(text_norm)
        return text_norm

    def filter_oov(self, text):
        val_chars = self._symbol_to_id
        txt_filt = "".join(list(filter(lambda x: x in val_chars, text)))
        # print(f"text after filtering OOV: {txt_filt}")
        return txt_filt

def preprocess_text(txt, text_mapper, hps, uroman_dir=None, lang=None):
    txt = preprocess_char(txt, lang=lang)
    is_uroman = hps.data.training_files.split('.')[-1] == 'uroman'
    if is_uroman:
        with tempfile.TemporaryDirectory() as tmp_dir:
            if uroman_dir is None:
                cmd = f"git clone git@github.com:isi-nlp/uroman.git {tmp_dir}"
                # print(cmd)
                subprocess.check_output(cmd, shell=True)
                uroman_dir = tmp_dir
            uroman_pl = os.path.join(uroman_dir, "bin", "uroman.pl")
            # print(f"uromanize")
            txt = text_mapper.uromanize(txt, uroman_pl)
            # print(f"uroman text: {txt}")
    txt = txt.lower()
    txt = text_mapper.filter_oov(txt)
    return txt



# txt = "Liputan6 . com , Jakarta : Presiden Susilo Bambang Yudhoyono menekankan bahwa tantangan terbesar yang dihadapi bangsa-bangsa Asia dan Afrika saat ini adalah masalah kemiskinan yang sangat buruk .Yudhoyono berharap masalah ini menjadi pembahasan penting dalam Konferensi Tingkat Tinggi Asia-Afrika .Demikian pidato Yudhoyono saat membuka KTT Asia-Afrika di Jakarta Convention Centre , Jakarta , Jumat ( 22/4 )"
# output_path = "generated_audio.mp3"


def vits_tts(txt, output_path, current_dir=""):

    if torch.cuda.is_available():
        device = torch.device("cuda")
    else:
        device = torch.device("cpu")

    # print(f"Run inference with {device}")

    LANG = "ind"
    ckpt_dir = LANG

    vocab_file = f"{current_dir}{ckpt_dir}/vocab.txt"
    config_file = f"{current_dir}{ckpt_dir}/config.json"
    # print(config_file)

    assert os.path.isfile(config_file), f"{config_file} doesn't exist"
    hps = utils.get_hparams_from_file(config_file)
    text_mapper = TextMapper(vocab_file)
    net_g = SynthesizerTrn(
        len(text_mapper.symbols),
        hps.data.filter_length // 2 + 1,
        hps.train.segment_size // hps.data.hop_length,
        **hps.model)
    net_g.to(device)
    _ = net_g.eval()

    g_pth = f"{current_dir}{ckpt_dir}/G_100000.pth"
    # print(f"load {g_pth}")

    _ = utils.load_checkpoint(g_pth, net_g, None)

    # print(f"text: {txt}")

    # Convert full stop (.) & comma (,) to character in vocab.txt
    txt = txt.replace(".", " _ ")
    txt = txt.replace(",", " - ")

    txt = preprocess_text(txt, text_mapper, hps, lang=LANG)
    stn_tst = text_mapper.get_text(txt, hps)
    with torch.no_grad():
        x_tst = stn_tst.unsqueeze(0).to(device)
        x_tst_lengths = torch.LongTensor([stn_tst.size(0)]).to(device)
        hyp = net_g.infer(
            x_tst, x_tst_lengths, noise_scale=.667,
            noise_scale_w=0.8, length_scale=1.0
        )[0][0,0].cpu().float().numpy()

    # # Cetak ke layar ipynb
    # print(f"Generated audio")
    # Audio(hyp, rate=hps.data.sampling_rate)

    # # Save audio to WAV file
    # audio_data = (hyp * 32767).astype(np.int16) # Convert audio data to integers (assuming 16-bit depth)
    # with wave.open("generated_audio.wav", "wb") as wav_file:
    #     wav_file.setnchannels(1)  # Mono audio
    #     wav_file.setsampwidth(2)  # 16-bit audio (2 bytes per sample)
    #     wav_file.setframerate(hps.data.sampling_rate)  # Set sample rate
    #     wav_file.writeframes(audio_data.tobytes())

    # Save audio to MP3 file
    sf.write(output_path, hyp, hps.data.sampling_rate)

    # print(f"Generated audio saved to {output_path}")


# vits_tts(txt, output_path)