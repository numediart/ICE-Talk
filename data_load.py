# -*- coding: utf-8 -*-
#!/usr/bin/env python2
'''
Adapted from original code by kyubyong park. kbpark.linguist@gmail.com. 
https://www.github.com/kyubyong/dc_tts
'''

from __future__ import print_function

import codecs
import re
import os
import glob
import unicodedata
import logging 
import sys

import numpy as np
import tensorflow as tf
import librosa

from libutil import basename, read_floats_from_8bit
from utils import load_spectrograms, end_pad_for_reduction_shape_sync, \
                    durations_to_hard_attention_matrix,  \
                    durations_to_position # durations_to_fractional_position,

from tqdm import tqdm
import pandas as pd
import ast

def load_vocab(hp):
    vocab = hp.vocab # default
    if 'speaker_dependent_phones' in hp.multispeaker:
        vocab = [hp.vocab[0]]
        for speaker in hp.speaker_list[1:]: ## assume first positions are just padding
            for phone in hp.vocab[1:]:
                vocab.append('%s_%s'%(phone, speaker))

    char2idx = {char: idx for idx, char in enumerate(vocab)}
    idx2char = {idx: char for idx, char in enumerate(vocab)}
    return char2idx, idx2char

def text_normalize(text, hp):
    text = ''.join(char for char in unicodedata.normalize('NFD', text)
                           if unicodedata.category(char) != 'Mn') # Strip accents

    text = text.lower()
    text = re.sub("[^{}]".format(hp.vocab), " ", text)
    text = re.sub("[ ]+", " ", text)
    return text

def phones_normalize(text, char2idx, speaker_code=''):
    phones = re.split('\s+', text.strip(' \n'))
    if speaker_code: # then make speaker-dependent phones
        phones = ['%s_%s'%(phone, speaker_code) for phone in phones]
    for phone in phones: 
        if phone not in char2idx:
            print(text)
            sys.exit('Phone %s not listed in phone set'%(phone))
    return phones

def text_to_phonetic(text='Hello world', festival_cmd='festival', id='test'):
    #import pdb;pdb.set_trace()
    import os
    if not os.path.exists('demo/'): os.makedirs('demo/')
    os.chdir('demo/')
    with open("utts.data", "w") as text_file:
        utt='('+id+' "'+text+'")'
        text_file.write(utt)
    SCRIPT="../script/festival/make_rich_phones_cmulex.scm"

    cmd=festival_cmd+' -b '+SCRIPT+" | grep ___KEEP___ | sed 's/___KEEP___//' | tee ./transcript_temp1.csv"
    os.system(cmd)

    cmd='python ../script/festival/fix_transcript.py ./transcript_temp1.csv > ./transcript.csv'
    os.system(cmd)

    os.chdir('..')

def load_data(hp, mode="train", audio_extension='.wav'):
    '''Loads data
      Args:
          mode: "train" / "validation" / "synthesis" / "demo".
    '''
    assert mode in ('train', 'synthesis', 'validation', 'demo')
    logging.info('Start loading data in mode: %s'%(mode))
    get_speaker_codes = ( hp.multispeaker != []) ## False if hp.multispeaker is empty list
    #import pdb;pdb.set_trace()
    dataset_df_path=os.path.join(hp.featuredir,'dataset_'+mode+'.csv')
    
    # In demo mode, we change the "dataset" with only one line each time and do not want to use always the same df
    #if os.path.exists(dataset_df_path) and mode != 'demo':
    if 0:
        dataset_df=pd.read_csv(dataset_df_path)
        
        dataset = {}
        #import pdb;pdb.set_trace()

        # this does not work in train mode because of  problem with doing pd.eval() with bytes
        try:
            dataset['texts'] = np.array([pd.eval(e) for e in dataset_df['texts'].tolist()])
        except AttributeError:
            #that is why we do this
            dataset['texts'] = np.array([ast.literal_eval(e) for e in dataset_df['texts'].tolist()])
            # I think this cause an error when trying training:
            # tensorflow.python.framework.errors_impl.InvalidArgumentError: Input to DecodeRaw has length 105 that is not a multiple of 4, the size of int32
            
        dataset['fpaths'] = dataset_df['fpaths'].tolist() ## at synthesis, fpaths only a way to get bases -- wav files probably do not exist
        dataset['text_lengths'] = dataset_df['text_lengths'].tolist() ## only used in training (where length information lost due to string format) - TODO: good motivation for this format?
        dataset['audio_lengths'] = dataset_df['audio_lengths'].tolist() ## might be []
        dataset['label_lengths'] = dataset_df['label_lengths'].tolist() ## might be []

        if get_speaker_codes:
            dataset['speakers'] = dataset_df['speakers'].tolist()
        if hp.use_external_durations:
            dataset['durations'] = dataset_df['durations'].tolist()

    else:
        if mode in ['synthesis', 'demo']: get_speaker_codes = False ## never read speaker from transcript for synthesis -- take user-specified speaker instead

        # Load vocabulary
        char2idx, idx2char = load_vocab(hp)

        if mode in ["train", "validation"]:
            transcript = os.path.join(hp.transcript)
        elif mode == 'synthesis':
            transcript = os.path.join(hp.test_transcript)
        else:
            transcript = './demo/transcript.csv'

        if hp.multispeaker:
            speaker2ix = dict(zip(hp.speaker_list, range(len(hp.speaker_list))))

        fpaths, text_lengths, texts, speakers, durations = [], [], [], [], []
        audio_lengths, label_lengths = [], []
        lines = codecs.open(transcript, 'r', 'utf-8').readlines()

        too_long_count_frames = 0
        too_long_count_text = 0
        no_data_count = 0

        nframes = 0 ## default 'False' value
        for line in tqdm(lines, desc='load_data'):
            line = line.strip('\n\r |')
            if line == '':
                continue
            fields = line.strip().split("|")

            assert len(fields) >= 1,  fields
            if len(fields) > 1:
                assert len(fields) >= 3,  fields

            fname = fields[0]
            if len(fields) > 1:
                unnorm_text, norm_text = fields[1:3]
            else:
                norm_text = None # to test if audio only

            if hp.validpatt: 
                if mode=="train": 
                    if hp.validpatt in fname:
                        continue
                elif mode=="validation":
                    if hp.validpatt not in fname:
                        continue

            

            if len(fields) >= 4:
                phones = fields[3]

            

            if norm_text is None:
                letters_or_phones = [] #  [0] ## dummy 'text' (1 character of padding) where we are using audio only
            elif hp.input_type == 'phones':
                if 'speaker_dependent_phones' in hp.multispeaker:
                    speaker_code = speaker
                else:
                    speaker_code = ''
                phones = phones_normalize(phones, char2idx, speaker_code=speaker_code) # in case of phones, all EOS markers are assumed included
                letters_or_phones = [char2idx[char] for char in phones]
            elif hp.input_type == 'letters':
                text = text_normalize(norm_text, hp) + "E"  # E: EOS
                letters_or_phones = [char2idx[char] for char in text]

            text_length = len(letters_or_phones)

            if text_length > hp.max_N:
                #print('number of letters/phones for %s is %s, exceeds max_N %s: skip it'%(fname, text_length, hp.max_N))
                too_long_count_text += 1
                continue


            if mode in ["train", "validation"] and os.path.exists(hp.coarse_audio_dir):
                mel = "{}/{}".format(hp.coarse_audio_dir, fname+".npy")
                if not os.path.exists(mel):
                    logging.debug('no file %s'%(mel))
                    no_data_count += 1
                    continue
                nframes = np.load(mel).shape[0]
                if nframes > hp.max_T:
                    #print('number of frames for %s is %s, exceeds max_T %s: skip it'%(fname, nframes, hp.max_T))
                    too_long_count_frames += 1
                    continue
                audio_lengths.append(nframes)

            texts.append(np.array(letters_or_phones, np.int32))

            fpath = os.path.join(hp.waveforms, fname + audio_extension)
            fpaths.append(fpath)
            text_lengths.append(text_length)     
            
            ## get speaker before phones in case need to get speaker-dependent phones
            if get_speaker_codes:
                assert len(fields) >= 5, fields            
                speaker = fields[4]
                speaker_ix = speaker2ix[speaker]
                speakers.append(np.array(speaker_ix, np.int32))    
                       

            if hp.merlin_label_dir: ## only get shape here -- get the data later
                try:
                    label_length, label_dim = np.load("{}/{}".format(hp.merlin_label_dir, basename(fpath)+".npy")).shape
                except TypeError:
                    label_length, label_dim = np.load("{}/{}".format(hp.merlin_label_dir, basename(fpath.decode('utf-8'))+".npy")).shape
                label_lengths.append(label_length)
                assert label_dim==hp.merlin_lab_dim

            if hp.use_external_durations:
                assert len(fields) >= 6, fields            
                duration_data = fields[5]
                duration_data = [int(value) for value in re.split('\s+', duration_data.strip(' '))]
                duration_data = np.array(duration_data, np.int32)
                if hp.merlin_label_dir:
                    duration_data = duration_data[duration_data > 0] ## merlin label contains no skipped items
                    assert len(duration_data) == label_length, (len(duration_data), label_length, fpath)
                else:
                    assert len(duration_data) == text_length, (len(duration_data), text_length, fpath)
                if nframes:
                    assert duration_data.sum() == nframes*hp.r, (duration_data.sum(), nframes*hp.r)
                durations.append(duration_data)             

            # !TODO! check this -- duplicated!?
            # if hp.merlin_label_dir: ## only get shape here -- get the data later
            #     label_length, _ = np.load("{}/{}".format(hp.merlin_label_dir, basename(fpath)+".npy")).shape
            #     label_lengths.append(label_length)

        #import pdb;pdb.set_trace()

        if mode=="validation":
            if len(texts)==0:
                logging.error('No validation sentences collected: maybe the validpatt %s matches no training data file names?'%(hp.validpatt)) ; sys.exit(1)

        logging.info ('Loaded data for %s sentences'%(len(texts)))
        logging.info ('Sentences skipped with missing features: %s'%(no_data_count))    
        logging.info ('Sentences skipped with > max_T (%s) frames: %s'%(hp.max_T, too_long_count_frames))
        logging.info ('Additional sentences skipped with > max_N (%s) letters/phones: %s'%(hp.max_N, too_long_count_text))
    
        if mode == 'train' and hp.n_utts > 0:
            n_utts = hp.n_utts
            assert n_utts <= len(fpaths)
            logging.info ('Take first %s (n_utts) sentences for training'%(n_utts))
            fpaths = fpaths[:n_utts]
            text_lengths = text_lengths[:n_utts]
            texts = texts[:n_utts]
            if get_speaker_codes:
                speakers = speakers[:n_utts]
            if audio_lengths:
                audio_lengths = audio_lengths[:n_utts]
            if label_lengths:
                label_lengths = label_lengths[:n_utts]
        
        
        if mode == 'train':
            ## Return string representation which will be parsed with tf's decode_raw:
            texts = [text.tostring() for text in texts] 
            if get_speaker_codes:
                speakers = [speaker.tostring() for speaker in speakers]      
            if hp.use_external_durations:
                durations = [d.tostring() for d in durations]   

        if mode in ['validation', 'synthesis', 'demo']:
            ## Prepare a batch of 'stacked texts' (matrix with number of rows==synthesis batch size, and each row an array of integers)
            stacked_texts = np.zeros((len(texts), hp.max_N), np.int32)
            for i, text in enumerate(texts):
                stacked_texts[i, :len(text)] = text
            texts = stacked_texts

            if hp.use_external_durations:
                stacked_durations = np.zeros((len(texts), hp.max_T, hp.max_N), np.int32)
                for i, dur in enumerate(durations):
                    duration_matrix = durations_to_hard_attention_matrix(dur)
                    duration_matrix = end_pad_for_reduction_shape_sync(duration_matrix, hp)
                    duration_matrix = duration_matrix[0::hp.r, :] 
                    m,n = duration_matrix.shape
                    stacked_durations[i, :m, :n] = duration_matrix            
                durations = stacked_durations


        dataset = {}
        dataset['texts'] = texts
        dataset['fpaths'] = fpaths ## at synthesis, fpaths only a way to get bases -- wav files probably do not exist
        dataset['text_lengths'] = text_lengths ## only used in training (where length information lost due to string format) - TODO: good motivation for this format?
        dataset['audio_lengths'] = audio_lengths ## might be []
        dataset['label_lengths'] = label_lengths ## might be []

        dataset_df=dataset.copy()

        try:
            dataset_df['texts']=dataset_df['texts'].tolist()
        except:
            # It is already a list
            pass
        try:
            if len(dataset_df['audio_lengths'])==0: dataset_df['audio_lengths']=[0]*len(dataset_df['texts'])
            if len(dataset_df['label_lengths'])==0: dataset_df['label_lengths']=[0]*len(dataset_df['texts'])
            if not os.path.exists(hp.featuredir): os.makedirs(hp.featuredir)
            pd.DataFrame.to_csv(pd.DataFrame.from_records(dataset_df), dataset_df_path) 
        except:
            import pdb;pdb.set_trace()

        if get_speaker_codes:
            dataset['speakers'] = speakers
        if hp.use_external_durations:
            dataset['durations'] = durations
    
    logging.info('Finished loading data in mode: %s'%(mode))
    #import pdb;pdb.set_trace()
    return dataset


def get_batch(hp, batchsize, dataset=None, data=None, model='t2m'):
    """Loads training data and put them in queues"""
    #import pdb;pdb.set_trace()
    #print ('get_batch')
    with tf.device('/cpu:0'):
        # Load data
        if dataset is None:
            #print('In get_batch: Load dataset')
            dataset = load_data(hp) 
        fpaths, text_lengths, texts = dataset['fpaths'], dataset['text_lengths'], dataset['texts']
        label_lengths, audio_lengths = dataset['label_lengths'], dataset['audio_lengths'] ## might be []

        # Calc total batch count
        num_batch = len(fpaths) // batchsize

        # Create Queues & parse -- TODO: deprecated!
        input_list = [fpaths, text_lengths, texts]
        if hp.multispeaker:
            input_list.append(dataset['speakers'])
        if hp.use_external_durations:
            input_list.append(dataset['durations'])
        if hp.merlin_label_dir:
            input_list.append(label_lengths)
        if audio_lengths:
            input_list.append(audio_lengths)

        sliced_data = tf.train.slice_input_producer(input_list, shuffle=True)
        fpath, text_length, text = sliced_data[:3]
        i = 3
        if hp.multispeaker:
            speaker = sliced_data[i] ; i+=1
            speaker = tf.decode_raw(speaker, tf.int32)
        if hp.use_external_durations:
            duration = sliced_data[i] ; i+=1
            duration = tf.decode_raw(duration, tf.int32)
        if hp.merlin_label_dir:
            label_length = sliced_data[i] ; i+=1
        if audio_lengths:
            audio_length = sliced_data[i] ; i+=1

        text = tf.decode_raw(text, tf.int32)  # (None,)

        if hp.use_external_durations:
            assert hp.random_reduction_on_the_fly ## The alternative is possible but not implemented.

        #pdb.set_trace()
        ## TODO: tf.py_func deprecated. https://www.tensorflow.org/api_docs/python/tf/py_func
        if hp.random_reduction_on_the_fly:

            assert os.path.isdir(hp.full_mel_dir)
            def _load_and_reduce_spectrograms(fpath):
                try:
                    fname = os.path.basename(fpath)
                except TypeError:
                    fname = os.path.basename(fpath.decode('utf-8'))
                try:
                    melfile = "{}/{}".format(hp.full_mel_dir, fname.replace("wav", "npy"))
                    if model=='ssrn': magfile = "{}/{}".format(hp.full_audio_dir, fname.replace("wav", "npy"))
                except TypeError:
                    # in python 3, we have to do this because of this: https://docs.python.org/3/howto/pyporting.html#text-versus-binary-data
                    melfile = "{}/{}".format(hp.full_mel_dir, fname.decode('utf-8').replace("wav", "npy"))
                    if model=='ssrn': magfile = "{}/{}".format(hp.full_audio_dir, fname.decode('utf-8').replace("wav", "npy"))
                mel = np.load(melfile)
                if model=='ssrn': mag = np.load(magfile)

                start = np.random.randint(0, hp.r, dtype=np.int16)

                mel = mel[start::hp.r, :]
                ### How it works:
                # >>> mel = np.arange(40)
                # >>> print mel[::4]
                # [ 0  4  8 12 16 20 24 28 32 36]
                # >>> print mel[0::4]
                # [ 0  4  8 12 16 20 24 28 32 36]
                # >>> print mel[1::4]
                # [ 1  5  9 13 17 21 25 29 33 37]
                # >>> print mel[2::4]
                # [ 2  6 10 14 18 22 26 30 34 38]
                # >>> print mel[3::4]
                # [ 3  7 11 15 19 23 27 31 35 39]

                ### need to pad end of mag accordingly (and trim start) so that it matches:--
                if model=='ssrn': mag = np.pad(mag, [[0, start], [0, 0]], mode="constant")[start:,:]
                else:
                    mag=np.float32(0.0)
                return fname, mel, mag, start

            ## Originally had these separate (see below) but couldn't find a 
            ## good way to pass random_start_position between the 2 places -- TODO - prune
            def _load_and_reduce_spectrograms_and_durations(fpath, duration):
                fname, mel, mag, random_start_position = _load_and_reduce_spectrograms(fpath)
                duration_matrix = durations_to_hard_attention_matrix(duration)
                duration_matrix = end_pad_for_reduction_shape_sync(duration_matrix, hp)
                duration_matrix = duration_matrix[random_start_position::hp.r, :]
                return fname, mel, mag, duration_matrix, random_start_position           
            def _load_and_reduce_spectrograms_and_durations_and_fractional_positions(fpath, duration):
                fname, mel, mag, duration_matrix, random_start_position = _load_and_reduce_spectrograms_and_durations(fpath, duration)
                positions = durations_to_position(duration, fractional=True)
                positions = end_pad_for_reduction_shape_sync(positions, hp)
                positions = positions[random_start_position::hp.r, :]                
                return fname, mel, mag, duration_matrix, positions
            def _load_and_reduce_spectrograms_and_durations_and_absolute_positions(fpath, duration):
                fname, mel, mag, duration_matrix, random_start_position = _load_and_reduce_spectrograms_and_durations(fpath, duration)
                positions = durations_to_position(duration, fractional=False)
                positions = end_pad_for_reduction_shape_sync(positions, hp)
                positions = positions[random_start_position::hp.r, :]                   
                return fname, mel, mag, duration_matrix, positions
            def _load_merlin_positions():
                try:
                    fname = os.path.basename(fpath)
                except TypeError:
                    fname = os.path.basename(fpath.decode('utf-8'))
                try:
                    merlin_position_file = "{}/{}".format(hp.merlin_position_dir, fname.replace("wav", "npy"))
                except TypeError:
                    # in python 3, we have to do this because of this: https://docs.python.org/3/howto/pyporting.html#text-versus-binary-data
                    merlin_position_file = "{}/{}".format(hp.merlin_position_dir, fname.decode('utf-8').replace("wav", "npy"))
                positions = np.load(merlin_position_file)
                return positions
            def _load_and_reduce_spectrograms_and_durations_and_merlin_positions(fpath, duration):
                fname, mel, mag, duration_matrix, random_start_position = _load_and_reduce_spectrograms_and_durations(fpath, duration)
                positions = _load_merlin_positions(fpath, hp)
                positions = end_pad_for_reduction_shape_sync(positions, hp)
                positions = positions[random_start_position::hp.r, :]                   
                return fname, mel, mag, duration_matrix, positions

            if hp.use_external_durations:
                if hp.history_type == 'fractional_position_in_phone':
                    fname, mel, mag, duration_matrix, position_in_phone = tf.py_func(_load_and_reduce_spectrograms_and_durations_and_fractional_positions, [fpath, duration], [tf.string, tf.float32, tf.float32, tf.float32, tf.float32])
                elif hp.history_type == 'absolute_position_in_phone':
                    fname, mel, mag, duration_matrix, position_in_phone = tf.py_func(_load_and_reduce_spectrograms_and_durations_and_absolute_positions, [fpath, duration], [tf.string, tf.float32, tf.float32, tf.float32, tf.float32])
                elif hp.history_type == 'merlin_position_from_file':
                    sys.exit('hp.history_type == "merlin_position_from_file" needs to be debugged')
                    fname, mel, mag, duration_matrix, position_in_phone = tf.py_func(_load_and_reduce_spectrograms_and_durations_and_merlin_positions, [fpath, duration], [tf.string, tf.float32, tf.float32, tf.float32, tf.float32])
                else:
                    fname, mel, mag, duration_matrix, _ = tf.py_func(_load_and_reduce_spectrograms_and_durations, [fpath, duration], [tf.string, tf.float32, tf.float32, tf.float32, tf.int16])
            else:
                fname, mel, mag, _ = tf.py_func(_load_and_reduce_spectrograms, [fpath], [tf.string, tf.float32, tf.float32, tf.int16])

        elif hp.prepro:
            #pdb.set_trace()
            def _load_spectrograms(fpath):
                #print('Load mel, mag from disk')
                try:
                    fname = os.path.basename(fpath)
                except TypeError:
                    fname = os.path.basename(fpath.decode('utf-8'))
                try:
                    mel = "{}/{}".format(hp.coarse_audio_dir, fname.replace("wav", "npy"))
                    if model=='ssrn': mag = "{}/{}".format(hp.full_audio_dir, fname.replace("wav", "npy"))
                except TypeError:
                    # in python 3, we have to do this because of this: https://docs.python.org/3/howto/pyporting.html#text-versus-binary-data
                    mel = "{}/{}".format(hp.coarse_audio_dir, fname.decode('utf-8').replace("wav", "npy"))
                    if model=='ssrn': mag = "{}/{}".format(hp.full_audio_dir, fname.decode('utf-8').replace("wav", "npy"))
                
                if 0:
                    if model=='ssrn': 
                        print ('mag file:')
                        print (mag)
                        print (np.load(mag).shape)
                if model!='ssrn': mag=np.float32(0.0)
                return fname, np.load(mel), np.load(mag)
            
            def _return_spectrograms(fpath):
                #print('Return mel, mag already in memory')
                try:
                    fname = os.path.basename(fpath)
                except TypeError:
                    fname = os.path.basename(fpath.decode('utf-8'))
                try:
                    #mel = "{}/{}".format(hp.coarse_audio_dir, fname.replace("wav", "npy"))
                    #mag = "{}/{}".format(hp.full_audio_dir, fname.replace("wav", "npy"))
                    mel=data['mel'][fname.split('.')[0]]
                    if model=='ssrn': mag=data['mag'][fname.split('.')[0]]
                except TypeError:
                    # in python 3, we have to do this because of this: https://docs.python.org/3/howto/pyporting.html#text-versus-binary-data
                    #mel = "{}/{}".format(hp.coarse_audio_dir, fname.decode('utf-8').replace("wav", "npy"))
                    #mag = "{}/{}".format(hp.full_audio_dir, fname.decode('utf-8').replace("wav", "npy"))
                    mel=data['mel'][fname.decode('utf-8').split('.')[0]]
                    if model=='ssrn': mag=data['mag'][fname.decode('utf-8').split('.')[0]]
                    #import pdb;pdb.set_trace()

                if 0:
                    if model=='ssrn': 
                        print ('mag file:')
                        print (mag)
                        print (mag.shape)
                if model!='ssrn': mag=np.float32(0.0)
                return fname, mel, mag
            #pdb.set_trace()
            if data is not None:
                fname, mel, mag = tf.py_func(_return_spectrograms, [fpath], [tf.string, tf.float32, tf.float32])
            else:
                fname, mel, mag = tf.py_func(_load_spectrograms, [fpath], [tf.string, tf.float32, tf.float32])

        else:
            fname, mel, mag = tf.py_func(load_spectrograms, [fpath], [tf.string, tf.float32, tf.float32])  # (None, n_mels)

        if hp.attention_guide_dir:
            def load_attention(fpath):
                try:
                    attention_guide_file = "{}/{}".format(hp.attention_guide_dir, basename(fpath)+".npy")
                except TypeError:
                    attention_guide_file = "{}/{}".format(hp.attention_guide_dir, basename(fpath.decode('utf-8'))+".npy")
                attention_guide = read_floats_from_8bit(attention_guide_file)
                return fpath, attention_guide
            _, attention_guide = tf.py_func(load_attention, [fpath], [tf.string, tf.float32]) # py_func wraps a python function and use it as a TensorFlow op.

        if hp.merlin_label_dir:
            def load_merlin_label(fpath):
                try:
                    label_file = "{}/{}".format(hp.merlin_label_dir, basename(fpath)+".npy")
                except TypeError:
                    label_file = "{}/{}".format(hp.merlin_label_dir, basename(fpath.decode('utf-8'))+".npy")
                label = np.load(label_file) ## TODO: could use read_floats_from_8bit format
                return fpath, label
            _, merlin_label = tf.py_func(load_merlin_label, [fpath], [tf.string, tf.float32]) # py_func wraps a python function and use it as a TensorFlow op.
            merlin_label.set_shape((None, hp.merlin_lab_dim))  ## will be phones x n_linguistic_features

        ### Earlier way to load durations (TODO - prune)
        # if hp.use_external_durations:
        #     def load_external_durations(duration):
        #         print ('load_external_durations')
        #         print (random_start_position)
        #         print (type(random_start_position))
        #         duration_matrix = durations_to_hard_attention_matrix(duration)
        #         duration_matrix = end_pad_for_reduction_shape_sync(duration_matrix, hp)
        #         if hp.random_reduction_on_the_fly:
        #             duration_matrix = duration_matrix[random_start_position::hp.r, :]
        #         else:
        #             duration_matrix = duration_matrix[0::hp.r, :]
        #         return duration_matrix
        #     [duration_matrix] = tf.py_func(load_external_durations, [duration], [tf.float32]) # py_func wraps a python function and use it as a TensorFlow op.

        # Add shape information
        fname.set_shape(())
        text.set_shape((None,))
        if hp.multispeaker:
            speaker.set_shape((None,))  ## 1D?
        if hp.use_external_durations:
            duration_matrix.set_shape((None,None))  ## will be letters x frames
        if hp.attention_guide_dir:
            attention_guide.set_shape((None,None))  ## will be letters x frames
        if hp.history_type == 'merlin_position_from_file':
            position_in_phone.set_shape((None, 9)) ## Always assume 9 positional features from merlin
        elif 'position_in_phone' in hp.history_type:
            position_in_phone.set_shape((None, 1))  ## frames x 1D
        mel.set_shape((None, hp.n_mels))
        if model=='ssrn': mag.set_shape((None, hp.full_dim))

        # Batching
        if model=='ssrn': 
            tensordict = {'text': text, 'mel': mel, 'mag': mag, 'fname': fname}
        else:
            tensordict = {'text': text, 'mel': mel, 'fname': fname}

        ## TODO: refactor to merge some of these blocks?

        if hp.multispeaker:
            tensordict['speaker'] = speaker  
        if hp.use_external_durations:
            tensordict['duration'] = duration_matrix
        if hp.attention_guide_dir:
            tensordict['attention_guide'] = attention_guide
        if hp.merlin_label_dir:
            tensordict['merlin_label'] = merlin_label
        if 'position_in_phone' in hp.history_type:
            tensordict['position_in_phone'] = position_in_phone
            
         
        if hp.bucket_data_by == 'audio_length':
            maxlen, minlen = max(audio_lengths), min(audio_lengths)
            sort_by_slice = audio_length
            logging.info('Bucket data by **audio** length')
        elif hp.bucket_data_by == 'text_length':
            if hp.merlin_label_dir:
                maxlen, minlen = max(label_lengths), min(label_lengths)
                sort_by_slice = label_length
                logging.info('Bucket data by **label** length')
            else:
                maxlen, minlen = max(text_lengths), min(text_lengths)
                sort_by_slice = text_length
                logging.info('Bucket data by **text** length')
        else:
            sys.exit('hp.bucket_data_by must be one of "audio_length", "text_length"')


        _, batched_tensor_dict = tf.contrib.training.bucket_by_sequence_length(             
                                            input_length=sort_by_slice,
                                            tensors=tensordict,
                                            batch_size=batchsize,
                                            bucket_boundaries=[i for i in range(minlen + 1, maxlen - 1, 20)],
                                            num_threads=hp.num_threads,
                                            capacity=batchsize*4,
                                            dynamic_pad=True)

        batched_tensor_dict['num_batch'] = num_batch        
        return batched_tensor_dict

