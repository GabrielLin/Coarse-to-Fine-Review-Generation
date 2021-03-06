import torch
import torch.nn as nn
from torch.autograd import Variable
from torch import optim
import torch.nn.functional as F
import torch.backends.cudnn as cudnn
from torch.nn.utils.rnn import pack_padded_sequence
from tensorboardX import SummaryWriter

import numpy as np 
import itertools
import random
import math
import sys
import os
import pickle
from tqdm import tqdm
from load import loadPrepareData
from load import SOS_ID, EOS_ID, PAD_ID
from model import TopicAttnDecoderRNN, SketchAttnDecoderRNN, ReviewAttnDecoderRNN, AttributeEncoder, EncoderRNN
from util import batch2TrainData
import time
from masked_cross_entropy import *

cudnn.benchmark = True
USE_CUDA = torch.cuda.is_available()

#############################################
# Training
#############################################

def adjust_learning_rate(optimizer, epoch, learning_rate, lr_decay_epoch, lr_decay_ratio):
    lr = learning_rate * (lr_decay_ratio ** (epoch // lr_decay_epoch))
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr
        
def from_pretrained(embeddings, freeze=True):
    assert embeddings.dim() == 2, 'Embeddings parameter is expected to be 2-dimensional'
    rows, cols = embeddings.shape
    embedding = torch.nn.Embedding(num_embeddings=rows, embedding_dim=cols)
    embedding.weight = torch.nn.Parameter(embeddings)
    embedding.weight.requires_grad = not freeze
    return embedding

def train(attr_input, topic_input, sketch_output, review_input, review_output, mask, encoder, birnn_encoder, review_decoder, 
            encoder_optimizer, birnn_encoder_optimizer, review_decoder_optimizer):

    encoder_optimizer.zero_grad()
    birnn_encoder_optimizer.zero_grad()
    review_decoder_optimizer.zero_grad()    # 梯度置零

    if USE_CUDA:
        attr_input = attr_input.cuda() 
        topic_input = topic_input.cuda()
        sketch_output = sketch_output.cuda()
        review_input = review_input.cuda()
        review_output = review_output.cuda()
        mask = mask.cuda()

    loss = 0
    print_losses = []

    # attribute encoder
    encoder_out, encoder_hidden = encoder(attr_input) # attribute encoder

    # review
    review_decoder_input = review_input 
    review_decoder_hidden = encoder_hidden[:review_decoder.n_layers]
    
    # use sketch encoder output to concatenate with review input
    sketch_birnn_output, _ = birnn_encoder(sketch_output)

    # review decoder
    review_decoder_output, review_decoder_hidden, _ = review_decoder(review_decoder_input, review_decoder_hidden, sketch_birnn_output, topic_input, sketch_output, encoder_out)
         
    mask_loss = masked_cross_entropy(review_decoder_output, review_output, mask)
    loss += mask_loss
    print_losses.append(mask_loss.data[0])
  
    loss.backward()  # BP process

    clip = 5.0
    ec = torch.nn.utils.clip_grad_norm(filter(lambda p: p.requires_grad, encoder.parameters()), clip)
    bc = torch.nn.utils.clip_grad_norm(filter(lambda p: p.requires_grad, birnn_encoder.parameters()), clip)
    dc = torch.nn.utils.clip_grad_norm(filter(lambda p: p.requires_grad, review_decoder.parameters()), clip)

    encoder_optimizer.step()
    birnn_encoder_optimizer.step()
    review_decoder_optimizer.step()

    return sum(print_losses) / len(print_losses)

def evaluate(attr_input, topic_input, sketch_output, review_input, review_output, mask, encoder, birnn_encoder, review_decoder, 
            encoder_optimizer, birnn_encoder_optimizer, review_decoder_optimizer):

    encoder.eval()
    birnn_encoder.eval()
    review_decoder.eval()

    if USE_CUDA:
        attr_input = attr_input.cuda() 
        topic_input = topic_input.cuda()
        sketch_output = sketch_output.cuda()
        review_input = review_input.cuda()
        review_output = review_output.cuda()
        mask = mask.cuda()

    loss = 0
    print_losses = []

    # attribute encoder
    encoder_out, encoder_hidden = encoder(attr_input) # attribute encoder

    # review
    review_decoder_input = review_input
    review_decoder_hidden = encoder_hidden[:review_decoder.n_layers]
    
    # use sketch encoder output to concatenate with review input
    sketch_birnn_output, _ = birnn_encoder(sketch_output)

    # review decoder
    review_decoder_output, review_decoder_hidden, _ = review_decoder(review_input, review_decoder_hidden, sketch_birnn_output, topic_input, sketch_output, encoder_out)
         
    mask_loss = masked_cross_entropy(review_decoder_output, review_output, mask)
    loss += mask_loss
    print_losses.append(mask_loss.data[0])

    return sum(print_losses) / len(print_losses)

def batchify(pairs, bsz, vocab, evaluation=False):
    # Work out how cleanly we can divide the dataset into bsz parts.
    nbatch = len(pairs) // bsz
    data = []
    for i in range(nbatch):
        data.append(batch2TrainData(vocab, pairs[i * bsz: i * bsz + bsz], evaluation))
    return data

def trainIters(corpus, learning_rate, lr_decay_epoch, lr_decay_ratio, batch_size, n_layers, hidden_size, 
        embed_size, attr_size, attr_num, overall, save_dir, loadFilename=None):
        
    print("corpus={}, learning_rate={}, lr_decay_epoch={}, lr_decay_ratio={}, batch_size={}, n_layers={}, \
    hidden_size={}, embed_size={}, attr_size={}, attr_num={}, overall={}, save_dir={}".format(corpus, learning_rate, \
    lr_decay_epoch, lr_decay_ratio, batch_size, n_layers, hidden_size, embed_size, attr_size, attr_num, overall, save_dir))

    print('load data...')
    vocab, train_pairs, valid_pairs, test_pairs = loadPrepareData(corpus, save_dir)  
    print('finish load data...')

    data_path = os.path.join(save_dir, "batches")
    # training data
    corpus_name = corpus
    training_batches = None
    try:
        training_batches = torch.load(os.path.join(data_path, '{}_{}.tar'.format('training_batches', batch_size)))
    except FileNotFoundError:
        print('Training pairs not found, generating ...')
        training_batches = batchify(train_pairs, batch_size, vocab)
        print('Complete building training pairs ...')
        torch.save(training_batches, os.path.join(data_path, '{}_{}.tar'.format('training_batches', batch_size)))

    # validation/test data
    eval_batch_size = 10
    try:
        val_batches = torch.load(os.path.join(data_path, '{}_{}.tar'.format('val_batches', eval_batch_size)))
    except FileNotFoundError:
        print('Validation pairs not found, generating ...')
        val_batches = batchify(valid_pairs, eval_batch_size, vocab, evaluation=True)  # 测试不需要求导
        print('Complete building validation pairs ...')
        torch.save(val_batches, os.path.join(data_path, '{}_{}.tar'.format('val_batches', eval_batch_size)))

    try:
        test_batches = torch.load(os.path.join(data_path, '{}_{}.tar'.format('test_batches', eval_batch_size)))
    except FileNotFoundError:
        print('Test pairs not found, generating ...')
        test_batches = batchify(test_pairs, eval_batch_size, vocab, evaluation=True)
        print('Complete building test pairs ...')
        torch.save(test_batches, os.path.join(data_path, '{}_{}.tar'.format('test_batches', eval_batch_size)))

    # aspect
    with open(os.path.join(save_dir, 'aspect_ids.pkl'), 'rb') as fp:
        ids = pickle.load(fp)

    # model
    checkpoint = None 
    print('Building encoder and decoder ...')

    # topic encoder
    with open(os.path.join(save_dir, 'user.pkl'), 'rb') as fp:
        user_dict = pickle.load(fp)
    with open(os.path.join(save_dir, 'item.pkl'), 'rb') as fp:
        item_dict = pickle.load(fp)
        
    num_user = len(user_dict)
    num_item = len(item_dict)
    num_over = overall 
    
    attr_embeddings = []
    uemb = nn.Embedding(num_user, attr_size)
    attr_embeddings.append(uemb)
    
    iemb = nn.Embedding(num_item, attr_size)
    attr_embeddings.append(iemb)
    
    remb = from_pretrained(torch.cat((torch.eye(num_over), torch.zeros(num_over, attr_size-num_over)), dim=1))
    attr_embeddings.append(remb)

    if USE_CUDA:
        for attr_embedding in attr_embeddings:
            attr_embedding = attr_embedding.cuda()

    encoder = AttributeEncoder(attr_size, attr_num, hidden_size, attr_embeddings, n_layers)

    # sketch encoder
    sketch_embedding = nn.Embedding(vocab.n_sketchs, embed_size)
    if USE_CUDA:
        sketch_embedding = sketch_embedding.cuda()

    birnn_encoder = EncoderRNN(embed_size, hidden_size, sketch_embedding, n_layers)

    # review decoder
    topic_embedding = nn.Embedding(vocab.n_topics, embed_size)
    sketch_embedding = nn.Embedding(vocab.n_sketchs, embed_size)
    word_embedding = nn.Embedding(vocab.n_words, embed_size)

    if USE_CUDA:
        topic_embedding = topic_embedding.cuda()
        sketch_embedding = sketch_embedding.cuda()
        word_embedding = word_embedding.cuda()

    aspect_ids = nn.Embedding(vocab.n_topics-3, 100)  # remove [SOS] [EOS] [PAD]
    aspect_ids.weight.data.copy_(torch.from_numpy(np.array(ids)))
    aspect_ids.weight.requires_grad = False 
    
    attn_model = 'dot'
    review_decoder = ReviewAttnDecoderRNN(topic_embedding, sketch_embedding, word_embedding, embed_size, hidden_size, attr_size, vocab.n_words, aspect_ids, n_layers)

    if loadFilename:
        checkpoint = torch.load(loadFilename)
        encoder.load_state_dict(checkpoint['encoder'])
        birnn_encoder.load_state_dict(checkpoint['birnn_encoder'])
        review_decoder.load_state_dict(checkpoint['review_decoder'])
        
    # use cuda
    if USE_CUDA:
        encoder = encoder.cuda()
        birnn_encoder = birnn_encoder.cuda()
        review_decoder = review_decoder.cuda()

    # optimizer
    print('Building optimizers ...')
    encoder_optimizer = optim.Adam(filter(lambda p: p.requires_grad, encoder.parameters()), lr=learning_rate)
    birnn_encoder_optimizer = optim.Adam(filter(lambda p: p.requires_grad, birnn_encoder.parameters()), lr=learning_rate)
    review_decoder_optimizer = optim.Adam(filter(lambda p: p.requires_grad, review_decoder.parameters()), lr=learning_rate)  
    
    if loadFilename:
        encoder_optimizer.load_state_dict(checkpoint['encoder_opt'])
        birnn_encoder_optimizer.load_state_dict(checkpoint['birnn_encoder_opt'])
        review_decoder_optimizer.load_state_dict(checkpoint['review_decoder_opt'])

    # initialize
    print('Initializing ...')
    step = 0
    epoch = 0
    perplexity = []
    _loss = []
    
    log_path = os.path.join('ckpt/' + corpus_name)
    if not os.path.exists(log_path):
        os.makedirs(log_path)
    writer = SummaryWriter(log_path)
    
    best_val_loss = None
    
    if loadFilename:
        step = checkpoint['step']
        epoch = checkpoint['epoch'] + 1
        perplexity = checkpoint['plt']
        _loss = checkpoint['loss']
        for i in range(len(_loss)):
            writer.add_scalar("Train/loss", _loss[i], i)
            writer.add_scalar("Train/perplexity", perplexity[i], i)

    while True:
        
        # learning rate adjust
        adjust_learning_rate(encoder_optimizer, epoch, learning_rate, lr_decay_epoch, lr_decay_ratio)
        adjust_learning_rate(birnn_encoder_optimizer, epoch, learning_rate, lr_decay_epoch, lr_decay_ratio)
        adjust_learning_rate(review_decoder_optimizer, epoch, learning_rate, lr_decay_epoch, lr_decay_ratio)
        
        # train epoch
        encoder.train()
        birnn_encoder.train()
        review_decoder.train()
        
        tr_loss = 0   
        for batch_idx, training_batch in enumerate(training_batches):
            attr_input, topic_input, sketch_output, review_input, review_output, mask = training_batch

            loss = train(attr_input, topic_input, sketch_output, review_input, review_output, mask, encoder, birnn_encoder, review_decoder, 
                            encoder_optimizer, birnn_encoder_optimizer, review_decoder_optimizer)
            step += 1

            tr_loss += loss
            
            _loss.append(loss)
            perplexity.append(math.exp(loss))    
            
            writer.add_scalar("Train/loss", loss, step)
            writer.add_scalar("Train/perplexity", math.exp(loss), step)
            
            print("epoch {} batch {} loss={} perplexity={} en_lr={:05.5f} bi_lr={:05.5f} de_lr={:05.5f}".format(epoch, batch_idx, 
            loss, math.exp(loss), encoder_optimizer.param_groups[0]['lr'], birnn_encoder_optimizer.param_groups[0]['lr'], 
            review_decoder_optimizer.param_groups[0]['lr']))
            
        cur_loss = tr_loss / len(training_batches)
        
        print('\n' + '-' * 30)
        print('train | epoch {:3d} | average loss {:5.5f} | average ppl {:8.3f}'.format(epoch, cur_loss, math.exp(cur_loss)))
        print('-' * 30)

        print_loss = 0
            
        # evaluate
        vl_loss = 0
        for val_batch in val_batches:
            attr_input, topic_input, sketch_output, review_input, review_output, mask = val_batch
            
            loss = evaluate(attr_input, topic_input, sketch_output, review_input, review_output, mask, encoder, birnn_encoder, review_decoder, 
                            encoder_optimizer, birnn_encoder_optimizer, review_decoder_optimizer)
            
            vl_loss += loss
        vl_loss /= len(val_batches)
        
        writer.add_scalar("Valid/loss", vl_loss, step)

        print('\n' + '-' * 30)
        print('valid | epoch {:3d} | valid loss {:5.5f} | valid ppl {:8.3f}'.format(epoch, vl_loss, math.exp(vl_loss)))
        print('-' * 30)
        
        # Save the model if the validation loss is the best we've seen so far.
        model_path = os.path.joion(save_dir, "model")
        if not best_val_loss or vl_loss < best_val_loss:
            directory = os.path.join(model_path, '{}_{}_{}'.format(n_layers, hidden_size, batch_size))
            if not os.path.exists(directory):
                os.makedirs(directory)
            torch.save({
                'step': step,
                'epoch': epoch,
                'encoder': encoder.state_dict(), 
                'encoder_opt': encoder_optimizer.state_dict(), 
                'birnn_encoder': birnn_encoder.state_dict(), 
                'birnn_encoder_opt': birnn_encoder_optimizer.state_dict(),
                'review_decoder': review_decoder.state_dict(),
                'review_decoder_opt': review_decoder_optimizer.state_dict(),
                'loss': _loss,
                'plt': perplexity
            }, os.path.join(directory, '{}_{}.tar'.format(epoch, 'review_model')))
            best_val_loss = vl_loss
     
            # Run on test data.
            ts_loss = 0
            for test_batch in test_batches:
                attr_input, topic_input, sketch_output, review_input, review_output, mask = test_batch

                loss = evaluate(attr_input, topic_input, sketch_output, review_input, review_output, mask, encoder, birnn_encoder, review_decoder, 
                            encoder_optimizer, birnn_encoder_optimizer, review_decoder_optimizer)
                
                ts_loss += loss
            ts_loss /= len(test_batches)
            writer.add_scalar("Test/loss", ts_loss, step)
            
            print('\n' + '-' * 30)
            print('| test loss {:5.2f} | test ppl {:8.2f}'.format(ts_loss, math.exp(ts_loss)))
            print('-' * 30 + '\n')

        
        if vl_loss > best_val_loss:
            print('validation loss is larger than best validation loss. Break!')
            break

        epoch += 1
        

