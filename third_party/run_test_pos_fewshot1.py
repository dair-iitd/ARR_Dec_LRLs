# coding=utf-8
# Copyright 2018 The Google AI Language Team Authors,
# The HuggingFace Inc. team, and The XTREME Benchmark Authors.
# Copyright (c) 2018, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Fine-tuning models for NER and POS tagging."""

from __future__ import absolute_import, division, print_function

import argparse
import glob
import logging
import os
import random
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import scipy
import torch
from transformers.adapters.composition import Fuse, Stack
from seqeval.metrics import precision_score, recall_score, f1_score
from tensorboardX import SummaryWriter
from torch.nn import CrossEntropyLoss
from torch.utils.data import DataLoader, TensorDataset
from torch.utils.data import RandomSampler, SequentialSampler
from torch.utils.data.distributed import DistributedSampler
from tqdm import tqdm, trange
from utils_tag import convert_examples_to_features
from utils_tag import get_labels
from utils_tag import read_examples_from_file
import pdb
import json 

from transformers import (
  AdamW,
  get_linear_schedule_with_warmup,
  WEIGHTS_NAME,
  AutoConfig,
  AutoModelForTokenClassification,
  AutoTokenizer,
  BertTokenizer,
  HfArgumentParser,
  MultiLingAdapterArguments,
  AdapterConfig,
  AdapterType,
)
#from xlm import XLMForTokenClassification

l2l_map={'en':'eng', 'is':'isl', 'de':'deu','fo':'fao', 'got':'got', 'gsw':'gsw', 'da':'dan', 'no':'nor', 'ru':'rus', 'cs':'ces', 'qpm':'bul', 'hsb':'hsb', 'orv':'chu', 'cu':'chu', 'bg':'bul', 'uk':'ukr', 'be':'bel', 'am':'amh','sw':'swa','wo':'wol'}
with open("lang2id.json", "r") as f:
  LANG2ID = json.load(f)
print(LANG2ID)
for k,v in l2l_map.items():
  LANG2ID[k] = LANG2ID[v]
logger = logging.getLogger(__name__)

def set_seed(args):
  random.seed(args.seed)
  np.random.seed(args.seed)
  torch.manual_seed(args.seed)
  if args.n_gpu > 0:
    torch.cuda.manual_seed_all(args.seed)

def train(args, train_dataset, model, tokenizer, labels, pad_token_label_id, lang_adapter_names, task_name, adap_ids, lang2id=LANG2ID, wandb=None):
  """Train the model."""
  if args.local_rank in [-1, 0]:
    tb_writer = SummaryWriter()

  args.train_batch_size = args.per_gpu_train_batch_size * max(1, args.n_gpu)
  train_sampler = RandomSampler(train_dataset) if args.local_rank == -1 else DistributedSampler(train_dataset)
  train_dataloader = DataLoader(train_dataset, sampler=train_sampler, batch_size=args.train_batch_size)

  if args.max_steps > 0:
    t_total = args.max_steps
    args.num_train_epochs = args.max_steps // (len(train_dataloader) // args.gradient_accumulation_steps) + 1
  else:
    t_total = len(train_dataloader) // args.gradient_accumulation_steps * args.num_train_epochs

  # Prepare optimizer and schedule (linear warmup and decay)
  no_decay = ["bias", "LayerNorm.weight"]
  optimizer_grouped_parameters = [
    {"params": [p for n, p in model.named_parameters() if not any(nd in n for nd in no_decay)],
     "weight_decay": args.weight_decay},
    {"params": [p for n, p in model.named_parameters() if any(nd in n for nd in no_decay)], "weight_decay": 0.0}
  ]
  optimizer = AdamW(optimizer_grouped_parameters, lr=args.learning_rate, eps=args.adam_epsilon)
  logging.info([n for (n, p) in model.named_parameters() if p.requires_grad])
  scheduler = get_linear_schedule_with_warmup(optimizer, num_warmup_steps=0.1*t_total, num_training_steps=t_total)
  if args.fp16:
    try:
      from apex import amp
    except ImportError:
      raise ImportError("Please install apex from https://www.github.com/nvidia/apex to use fp16 training.")
    model, optimizer = amp.initialize(model, optimizer, opt_level=args.fp16_opt_level)

  # multi-gpu training (should be after apex fp16 initialization)
  if args.n_gpu > 1:
    model = torch.nn.DataParallel(model)

  # Distributed training (should be after apex fp16 initialization)
  if args.local_rank != -1:
    model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.local_rank],
                              output_device=args.local_rank,
                              find_unused_parameters=True)

  # Train!
  logger.info("***** Running training *****")
  logger.info("  Num examples = %d", len(train_dataset))
  logger.info("  Num Epochs = %d", args.num_train_epochs)
  logger.info("  Instantaneous batch size per GPU = %d", args.per_gpu_train_batch_size)
  logger.info("  Total train batch size (w. parallel, distributed & accumulation) = %d",
        args.train_batch_size * args.gradient_accumulation_steps * (
          torch.distributed.get_world_size() if args.local_rank != -1 else 1))
  logger.info("  Gradient Accumulation steps = %d", args.gradient_accumulation_steps)
  logger.info("  Total optimization steps = %d", t_total)

  best_score = 0.0
  best_checkpoint = None
  patience = 0
  global_step = 0
  tr_loss, logging_loss = 0.0, 0.0
  model.zero_grad()
  train_iterator = trange(int(args.num_train_epochs), desc="Epoch", disable=args.local_rank not in [-1, 0])
  set_seed(args) # Add here for reproductibility (even between python 2 and 3)

  cur_epoch = 0
  for _ in train_iterator:
    epoch_iterator = tqdm(train_dataloader, desc="Iteration", disable=args.local_rank not in [-1, 0])
    cur_epoch += 1
    for step, batch in enumerate(epoch_iterator):
      batch = tuple(t.to(args.device) for t in batch if t is not None)
      #pdb.set_trace()
      #print(batch[-1])
      if args.l2v:
        batch_size_ = batch[0].shape[0]
        inputs = {"input_ids": torch.cat((batch[0],batch[-1], adap_ids.repeat(batch_size_,1).to('cuda')),1),
            "attention_mask": batch[1],            
            "labels": batch[3]}
        #pdb.set_trace()
      else:
        inputs = {"input_ids": batch[0],
              "attention_mask": batch[1],
              "labels": batch[3]}
      if args.model_type != "distilbert":
        # XLM and RoBERTa don"t use segment_ids
        inputs["token_type_ids"] = batch[2] if args.model_type in ["bert", "xlnet"] else None

      if args.model_type == "xlm":
        pdb.set_trace()
        inputs["langs"] = batch[4]
      
      outputs,_ = model(**inputs)

      loss = outputs[0]

      if args.n_gpu > 1:
        # mean() to average on multi-gpu parallel training
        loss = loss.mean()
      if args.gradient_accumulation_steps > 1:
        loss = loss / args.gradient_accumulation_steps

      if args.fp16:
        with amp.scale_loss(loss, optimizer) as scaled_loss:
          scaled_loss.backward()
      else:
        loss.backward()
      tr_loss += loss.item()
      if (step + 1) % args.gradient_accumulation_steps == 0:
        if args.fp16:
          torch.nn.utils.clip_grad_norm_(amp.master_params(optimizer), args.max_grad_norm)
        else:
          torch.nn.utils.clip_grad_norm_(model.parameters(), args.max_grad_norm)

        optimizer.step()  # Update learning rate schedule
        scheduler.step()
        model.zero_grad()
        global_step += 1

        if args.local_rank in [-1, 0] and args.logging_steps > 0 and global_step % args.logging_steps == 0:
          # Log metrics
          #wandb.log({'loss':logging_loss})
          if args.local_rank == -1 and args.evaluate_during_training:
            # Only evaluate on single GPU otherwise metrics may not average well
            results, _ = evaluate(args, model, tokenizer, labels, pad_token_label_id, mode="dev", lang=args.train_langs, adap_ids=adap_ids, lang2id=lang2id, lang_adapter_names=lang_adapter_names, task_name=task_name)
            for key, value in results.items():
              tb_writer.add_scalar("eval_{}".format(key), value, global_step)
          tb_writer.add_scalar("lr", scheduler.get_lr()[0], global_step)
          tb_writer.add_scalar("loss", (tr_loss - logging_loss) / args.logging_steps, global_step)
          logging_loss = tr_loss
        #pdb.set_trace()
        # if global_step == 1:
        #   output_dir = os.path.join(args.output_dir, "checkpoint-best-0")
        #   if not os.path.exists(output_dir):
        #     os.makedirs(output_dir)
        #   model_to_save = model.module if hasattr(model, "module") else model
        #   model_to_save.save_all_adapters(output_dir)
        #   model_to_save.save_all_adapter_fusions(output_dir)
        #   model_to_save.save_pretrained(output_dir)
        #   tokenizer.save_pretrained(output_dir)

      if args.max_steps > 0 and global_step > args.max_steps:
        epoch_iterator.close()
        break
    if args.max_steps > 0 and global_step > args.max_steps:
      train_iterator.close()
      break

  if args.local_rank in [-1, 0]:
    tb_writer.close()
  model_to_save = model.module if hasattr(model, "module") else model
  model_to_save.save_all_adapters(args.write_dir)
  model_to_save.save_all_adapter_fusions(args.write_dir)
  model_to_save.save_pretrained(args.write_dir)
  tokenizer.save_pretrained(args.write_dir)
  torch.save(args, os.path.join(args.write_dir, "training_args.bin"))
  return global_step, tr_loss / global_step

def evaluate(args, model, tokenizer, labels, pad_token_label_id, mode, prefix="", lang="en", adap_ids=None, lang2id=None, print_result=True, adapter_weight=None, lang_adapter_names=None, task_name=None, calc_weight_step=0):
  eval_dataset = load_and_cache_examples(args, tokenizer, labels, pad_token_label_id, mode=mode, lang=lang, lang2id=lang2id)

  args.eval_batch_size = args.per_gpu_eval_batch_size * max(1, args.n_gpu)
  # Note that DistributedSampler samples randomly
  eval_sampler = SequentialSampler(eval_dataset) if args.local_rank == -1 else DistributedSampler(eval_dataset)
  eval_dataloader = DataLoader(eval_dataset, sampler=eval_sampler, batch_size=args.eval_batch_size)

  # multi-gpu evaluate
  if args.n_gpu > 1:
    model = torch.nn.DataParallel(model)
  # Eval!
  logger.info("***** Running evaluation %s in %s *****" % (prefix, lang))
  logger.info("  Num examples = %d", len(eval_dataset))
  logger.info("  Batch size = %d", args.eval_batch_size)
  eval_loss = 0.0
  nb_eval_steps = 0
  preds = None
  out_label_ids = None
  model.eval()
  weights1 = []
  masks = []
  weights_dict = {}
  for batch in tqdm(eval_dataloader, desc="Evaluating"):
    batch = tuple(t.to(args.device) for t in batch)

    if calc_weight_step > 0:
      pdb.set_trace()
      adapter_weight = calc_weight_multi(args, model, batch, lang_adapter_names, task_name, adapter_weight, 0)
    with torch.no_grad():
      if args.l2v:
        batch_size_ = batch[0].shape[0]
        inputs = {"input_ids": torch.cat((batch[0],batch[-1], adap_ids.repeat(batch_size_,1).to('cuda:0')),1),
            "attention_mask": batch[1],            
            "labels": batch[3]}
        masks += [it for it in batch[1]]
        
      else:
        inputs = {"input_ids": batch[0],
              "attention_mask": batch[1],
              "labels": batch[3]}

      if args.model_type != "distilbert":
        # XLM and RoBERTa don"t use segment_ids
        inputs["token_type_ids"] = batch[2] if args.model_type in ["bert", "xlnet"] else None
      if args.model_type == 'xlm':
        inputs["langs"] = batch[4]
      outputs, adapter_weights= model(**inputs)
      #for it_ in range(12):
      #  print(adapter_weights[it_][0][0],adapter_weights[it_][1][0],adapter_weights[it_][1][-1]) 
      for i in range(12):
        x,_ = torch.split(adapter_weights[i],batch_size_,0)
        
        if i not in weights_dict:
          weights_dict[i] = []
        #pdb.set_trace()
        weights_dict[i] += [it for it in x]
        #torch.save(y[0], "{}_l2v_l{}.ckpt".format(lang,str(i)))
      tmp_eval_loss, logits = outputs[:2]

      if args.n_gpu > 1:
        # mean() to average on multi-gpu parallel evaluating
        tmp_eval_loss = tmp_eval_loss.mean()

      eval_loss += tmp_eval_loss.item()
    nb_eval_steps += 1
    if preds is None:
      preds = logits.detach().cpu().numpy()
      out_label_ids = inputs["labels"].detach().cpu().numpy()
    else:
      preds = np.append(preds, logits.detach().cpu().numpy(), axis=0)
      out_label_ids = np.append(out_label_ids, inputs["labels"].detach().cpu().numpy(), axis=0)

  #pdb.set_trace()
  #torch.save(masks, "{}_masks.ckpt".format(lang))
  #for i in range(12):
  #  torch.save(weights_dict[i], "{}_fusion_l{}.ckpt".format(lang,str(i)))
  if nb_eval_steps == 0:
    results = {k: 0 for k in ["loss", "precision", "recall", "f1"]}
  else:
    eval_loss = eval_loss / nb_eval_steps
    preds = np.argmax(preds, axis=2)

    label_map = {i: label for i, label in enumerate(labels)}

    out_label_list = [[] for _ in range(out_label_ids.shape[0])]
    preds_list = [[] for _ in range(out_label_ids.shape[0])]

    for i in range(out_label_ids.shape[0]):
      for j in range(out_label_ids.shape[1]):
        if out_label_ids[i, j] != pad_token_label_id:
          out_label_list[i].append(label_map[out_label_ids[i][j]])
          preds_list[i].append(label_map[preds[i][j]])

    results = {
      "loss": eval_loss,
      "precision": precision_score(out_label_list, preds_list),
      "recall": recall_score(out_label_list, preds_list),
      "f1": f1_score(out_label_list, preds_list)
    }

  if print_result:
    logger.info("***** Evaluation result %s in %s *****" % (prefix, lang))
    for key in sorted(results.keys()):
      logger.info("  %s = %s", key, str(results[key]))
  return results, preds_list



def load_and_cache_examples(args, tokenizer, labels, pad_token_label_id, mode, lang, lang2id=LANG2ID, few_shot=-1):
  # Make sure only the first process in distributed training process
  # the dataset, and the others will use the cache
  labels2labels = {1:0, 2:1, 3:2, 5:3, 6:4, 7:5, 8:6, -100:-100}
  if args.local_rank not in [-1, 0] and not evaluate:
    torch.distributed.barrier()

  # Load data features from cache or dataset file
  bpe_dropout = args.bpe_dropout
  if mode != 'train': bpe_dropout = 0
  if bpe_dropout > 0:
    cached_features_file = os.path.join(args.data_dir, "cached_{}_{}_{}_{}_drop{}".format(mode, lang,
      list(filter(None, args.model_name_or_path.split("/"))).pop(),
      str(args.max_seq_length), bpe_dropout))
  else:
    cached_features_file = os.path.join(args.data_dir, "cached_{}_{}_{}_{}".format(mode, lang,
      list(filter(None, args.model_name_or_path.split("/"))).pop(),
      str(args.max_seq_length)))
  if os.path.exists(cached_features_file) and not args.overwrite_cache:
    logger.info("Loading features from cached file %s", cached_features_file)
    features = torch.load(cached_features_file)
  else:
    langs = lang.split(',')
    logger.info("all languages = {}".format(lang))
    features = []
    for lg in langs:
      data_file = os.path.join(args.data_dir, lg, "{}.{}".format(mode, args.model_name_or_path))
      logger.info("Creating features from dataset file at {} in language {}".format(data_file, lg))
      examples = read_examples_from_file(data_file, lg, LANG2ID)
      #pdb.set_trace()
      features_lg = convert_examples_to_features(examples, labels, args.max_seq_length, tokenizer,
                          cls_token_at_end=bool(args.model_type in ["xlnet"]),
                          cls_token=tokenizer.cls_token,
                          cls_token_segment_id=2 if args.model_type in ["xlnet"] else 0,
                          sep_token=tokenizer.sep_token,
                          sep_token_extra=bool(args.model_type in ["roberta", "xlmr"]),
                          pad_on_left=bool(args.model_type in ["xlnet"]),
                          pad_token=tokenizer.convert_tokens_to_ids([tokenizer.pad_token])[0],
                          pad_token_segment_id=4 if args.model_type in ["xlnet"] else 0,
                          pad_token_label_id=pad_token_label_id,
                          lang=lg,
                          bpe_dropout=bpe_dropout,
                          )
      features.extend(features_lg)
    if args.local_rank in [-1, 0]:
      logger.info("Saving features into cached file {}, len(features)={}".format(cached_features_file, len(features)))
      torch.save(features, cached_features_file)

  # Make sure only the first process in distributed training process
  # the dataset, and the others will use the cache
  if args.local_rank == 0 and not evaluate:
    torch.distributed.barrier()

  if few_shot > 0 and mode == 'train':
    logger.info("Original no. of examples = {}".format(len(features)))
    #features = features[: few_shot]
    fewshot_features_file = os.path.join(args.data_dir, "fewShotcached_{}_{}_{}_{}_size{}".format(mode, lang,
      list(filter(None, args.model_name_or_path.split("/"))).pop(),
      str(args.max_seq_length), few_shot))
    #pdb.set_trace()
    features = torch.load(fewshot_features_file)
    logger.info('Using few-shot learning on {} examples'.format(len(features)))

  # Convert to Tensors and build dataset
  all_input_ids = torch.tensor([f.input_ids for f in features], dtype=torch.long)
  all_input_mask = torch.tensor([f.input_mask for f in features], dtype=torch.long)
  all_segment_ids = torch.tensor([f.segment_ids for f in features], dtype=torch.long)
  all_label_ids = torch.tensor([f.label_ids for f in features], dtype=torch.long)
  if mode=='train':
    #pdb.set_trace()
    if args.map_label:
      pdb.set_trace()
      all_label_ids.apply_(lambda x: labels2labels[x])
  if args.l2v and features[0].langs is not None:
    all_langs = torch.tensor([f.langs for f in features], dtype=torch.long)
    logger.info('all_langs[0] = {}'.format(all_langs[0]))
    dataset = TensorDataset(all_input_ids, all_input_mask, all_segment_ids, all_label_ids, all_langs)
    #print(all_langs)
  else:
    dataset = TensorDataset(all_input_ids, all_input_mask, all_segment_ids, all_label_ids)
  return dataset


@dataclass
class ModelArguments:
    """
    Arguments pertaining to which model/config/tokenizer we are going to fine-tune from.
    """

    model_name_or_path: str = field(
        metadata={"help": "Path to pretrained model or model identifier from huggingface.co/models"}
    )
    model_type: Optional[str] = field(
        default=None, metadata={"help": "Pretrained config name or path if not the same as model_name"}
    )
    config_name: Optional[str] = field(
        default=None, metadata={"help": "Pretrained config name or path if not the same as model_name"}
    )
    tokenizer_name: Optional[str] = field(
        default=None, metadata={"help": "Pretrained tokenizer name or path if not the same as model_name"}
    )
    cache_dir: Optional[str] = field(
        default=None, metadata={"help": "Where do you want to store the pretrained models downloaded from s3"}
    )
    labels: str = field(
        default=None, metadata={"help": "Where do you want to store the pretrained models downloaded from s3"}
    )
    data_dir: str = field(
        default=None, metadata={"help": "Where do you want to store the pretrained models downloaded from s3"}
    )
    output_dir: str = field(
        default=None, metadata={"help": "Where do you want to store the pretrained models downloaded from s3"}
    )
    write_dir: str = field(
        default=None, metadata={"help": "Where do you want to store the pretrained models downloaded from s3"}
    )
    max_seq_length: Optional[int] = field(
        default=128, metadata={"help": "Where do you want to store the pretrained models downloaded from s3"}
    )
    map_label: Optional[bool] = field(default=False)
    do_train: Optional[bool] = field(default=False )
    do_eval: Optional[bool] = field(default=False )
    do_predict: Optional[bool] = field(default=False )
    do_adapter_predict: Optional[bool] = field(default=False )
    do_predict_dev: Optional[bool] = field(default=False )
    do_predict_train: Optional[bool] = field(default=False )
    init_checkpoint: Optional[str] = field(default=None )
    evaluate_during_training: Optional[bool] = field(default=False )
    do_lower_case: Optional[bool] = field(default=False )
    few_shot: Optional[int] = field(default=-1 )
    per_gpu_train_batch_size: Optional[int] = field(default=8)
    per_gpu_eval_batch_size: Optional[int] = field(default=8)
    gradient_accumulation_steps: Optional[int] = field(default=1)
    learning_rate: Optional[float] = field(default=5e-5)
    weight_decay: Optional[float] = field(default=0.0)
    adam_epsilon: Optional[float] = field(default=1e-8)
    max_grad_norm: Optional[float] = field(default=1.0)
    num_train_epochs: Optional[float] = field(default=3.0)
    max_steps: Optional[int] = field(default=-1)
    save_steps: Optional[int] = field(default=-1)
    warmup_steps: Optional[int] = field(default=0)
    logging_steps: Optional[int] = field(default=50)
    save_only_best_checkpoint: Optional[bool] = field(default=False)
    eval_all_checkpoints: Optional[bool] = field(default=False)
    no_cuda: Optional[bool] = field(default=False)
    overwrite_output_dir: Optional[bool] = field(default=False)
    overwrite_cache: Optional[bool] = field(default=False)
    seed: Optional[int] = field(default=42)
    fp16: Optional[bool] = field(default=False)
    fp16_opt_level: Optional[str] = field(default="O1")
    local_rank: Optional[int] = field(default=-1)
    server_ip: Optional[str] = field(default="")
    server_port: Optional[str] = field(default="")
    predict_langs: Optional[str] = field(default="en")
    train_langs: Optional[str] = field(default="en")
    log_file: Optional[str] = field(default=None)
    eval_patience: Optional[int] = field(default=-1)
    bpe_dropout: Optional[float] = field(default=0)
    do_save_adapter_fusions: Optional[bool] = field(default=False)
    do_save_full_model: Optional[bool] = field(default=False)
    do_save_adapters: Optional[bool] = field(default=False)
    task_name: Optional[str] = field(default="ner")
    l2v: Optional[bool] = field(default=False)
    madx2: Optional[bool] = field(default=False)
    predict_task_adapter: Optional[str] = field(default=None)
    predict_lang_adapter: Optional[str] = field(default=None)
    test_adapter: Optional[bool] = field(default=False)
    rf: Optional[int] = field(default=4)
    adapter_weight: Optional[str] = field(default=None)

    calc_weight_step: Optional[int] = field(default=0)
    predict_save_prefix: Optional[str] = field(default=None)

# def setup_adapter(args, adapter_args, model, train_adapter=True, load_adapter=None, load_lang_adapter=None):
#   task_name = "cpg"
#   task_adapter_config = AdapterConfig.load(
#           adapter_args.adapter_config,
#           non_linearity=adapter_args.adapter_non_linearity,
#           reduction_factor=args.rf,
#   )
#   pdb.set_trace()
#   model.load_adapter(load_adapter, AdapterType.text_task, config=task_adapter_config)
#   #model.train_cpg_adapter([task_name])
#   model.set_active_adapters([task_name])
#   return model, task_name

def setup_adapter(args, adapter_args, model, train_adapter=True, load_adapter=None, load_lang_adapter=None,config=None):
  cpg_name = "cpg"
  task_name="ner"
  # pdb.set_trace()
  # load1_= "/".join(load_adapter.split('//')[:-1])+"/"+task_name
  # task_adapter_config = AdapterConfig.load(
  #          adapter_args.adapter_config,
  #         non_linearity=adapter_args.adapter_non_linearity,
  #         reduction_factor=2,
  # )
  pdb.set_trace()
  model.load_adapter(load_adapter, AdapterType.text_task, config=task_adapter_config)
  model.load_adapter(load1_, AdapterType.text_task, config=task_adapter_config)
  # model.load_adapter(load_adapter, AdapterType.text_task, config=task_adapter_config)
  model.set_active_adapters([[cpg_name], [task_name]])
  return model, task_name


def main():
  parser = argparse.ArgumentParser()

  parser = HfArgumentParser((ModelArguments, MultiLingAdapterArguments))
  args, adapter_args = parser.parse_args_into_dataclasses()


  if os.path.exists(args.output_dir) and os.listdir(
      args.output_dir) and args.do_train and not args.overwrite_output_dir:
    raise ValueError(
      "Output directory ({}) already exists and is not empty. Use --overwrite_output_dir to overcome.".format(
        args.output_dir))

  # Setup distant debugging if needed
  if args.server_ip and args.server_port:
    import ptvsd
    print("Waiting for debugger attach")
    ptvsd.enable_attach(address=(args.server_ip, args.server_port), redirect_output=True)
    ptvsd.wait_for_attach()

  # Setup CUDA, GPU & distributed training
  if args.local_rank == -1 or args.no_cuda:
    device = torch.device("cuda" if torch.cuda.is_available() and not args.no_cuda else "cpu")
    args.n_gpu = torch.cuda.device_count()
  else:
  # Initializes the distributed backend which sychronizes nodes/GPUs
    torch.cuda.set_device(args.local_rank)
    device = torch.device("cuda", args.local_rank)
    torch.distributed.init_process_group(backend="nccl")
    args.n_gpu = 1
  args.device = device

  # Setup logging
  logging.basicConfig(handlers = [logging.FileHandler(args.log_file), logging.StreamHandler()],
                      format = '%(asctime)s - %(levelname)s - %(name)s -   %(message)s',
                      datefmt = '%m/%d/%Y %H:%M:%S',
                      level = logging.INFO if args.local_rank in [-1, 0] else logging.WARN)
  logging.info("Input args: %r" % args)
  logger.warning("Process rank: %s, device: %s, n_gpu: %s, distributed training: %s, 16-bits training: %s",
           args.local_rank, device, args.n_gpu, bool(args.local_rank != -1), args.fp16)

  # Set seed
  set_seed(args)

  # Prepare NER/POS task
  labels = get_labels(args.labels)
  num_labels = len(labels)
  # Use cross entropy ignore index as padding label id
  # so that only real label ids contribute to the loss later
  pad_token_label_id = CrossEntropyLoss().ignore_index

  # Load pretrained model and tokenizer
  # Make sure only the first process in distributed training loads model/vocab
  if args.local_rank not in [-1, 0]:
    torch.distributed.barrier()
  
  config = AutoConfig.from_pretrained(
      args.config_name if args.config_name else args.model_name_or_path,
      num_labels=num_labels,
      cache_dir=args.cache_dir,
  )
  #config = AutoConfig.from_pretrained(args.output_dir)
  config.CPG = False
  args.model_type = config.model_type
  #tokenizer = AutoTokenizer.from_pretrained(args.model_name_or_path)
  tokenizer = AutoTokenizer.from_pretrained(args.output_dir)

  logger.info("loading from existing model {}".format(args.model_name_or_path))
  #pdb.set_trace()
  # model = AutoModelForTokenClassification.from_pretrained(
  #       args.model_name_or_path,
  #       config=config,
  #       cache_dir=args.cache_dir,
  #   )
  model = AutoModelForTokenClassification.from_pretrained(args.output_dir,config=config)
  #pdb.set_trace()
  lang2id = LANG2ID if args.l2v else None
  logger.info("Using lang2id = {}".format(lang2id))

  # Make sure only the first process in distributed training loads model/vocab
  if args.local_rank == 0:
    torch.distributed.barrier()
  model.to(args.device)
  # tokenizer = AutoTokenizer.from_pretrained(args.output_dir, do_lower_case=args.do_lower_case, use_fast=False)

  #pdb.set_trace()
  logger.info("Training/evaluation parameters %s", args)

  # Initialization for evaluation
  results = {}
  
  best_checkpoint = args.output_dir
  best_f1 = 0

  logger.info("Loading the best checkpoint from {}\n".format(best_checkpoint))
  
  load_lang_adapter = args.predict_lang_adapter
  model.model_name = args.model_name_or_path
  # model, task_name = setup_adapter(args, adapter_args, model, load_adapter=load_adapter, load_lang_adapter=load_lang_adapter, config=config)
  #cpg_name = "cpg"
  task_name="ner"
  load_adapter = best_checkpoint + "/" + task_name
  print(load_adapter)
  if args.madx2:
    #pdb.set_trace()
    leave_out = [len(model.roberta.encoder.layer)-1]
    task_adapter_config = AdapterConfig.load(
             adapter_args.adapter_config,
            non_linearity=adapter_args.adapter_non_linearity,
            reduction_factor=args.rf,
            leave_out = leave_out,
    )
  else:
    task_adapter_config = AdapterConfig.load(
             adapter_args.adapter_config,
            non_linearity=adapter_args.adapter_non_linearity,
            reduction_factor=args.rf,
    )
  # load a set of language adapters
  #logging.info("loading lang adpater {}".format(adapter_args.load_lang_adapter))
  # resolve the language adapter config
  if args.madx2:
    #pdb.set_trace()
    lang_adapter_config = AdapterConfig.load(
        adapter_args.lang_adapter_config,
        non_linearity=adapter_args.lang_adapter_non_linearity,
        reduction_factor=adapter_args.lang_adapter_reduction_factor,
        leave_out = leave_out,
    )
  else:
    lang_adapter_config = AdapterConfig.load(
        adapter_args.lang_adapter_config,
        non_linearity=adapter_args.lang_adapter_non_linearity,
        reduction_factor=adapter_args.lang_adapter_reduction_factor,
    )
  # load the language adapter from Hub
  languages = adapter_args.language.split(",")
  #adapter_names = adapter_args.load_lang_adapter.split(",")
  #assert len(languages) == len(adapter_names)
  lang_adapter_names = []
  #pdb.set_trace()
  for language in languages:
    print(language)
    # #pdb.set_trace()
    # lang_adapter_name = model.load_adapter(adapter_name,
    #     AdapterType.text_lang,
    #     config=lang_adapter_config,
    #     load_as=language,
    # )
    #pdb.set_trace()
    #lang_adapter_name = model.load_adapter(language+"/wiki@ukp")
    lang_adapter_name = model.load_adapter("/".join(load_adapter.split("/")[:-1])+language+"/")
    lang_adapter_names.append(lang_adapter_name)

  
  adapter_setup_ = Fuse('en','ru','cs')
  model.add_adapter_fusion(adapter_setup_)
  model.add_adapter(task_name, config=task_adapter_config)
  model.train_adapter_fusion_TA([task_name], adapter_setup_)

  fusion_path_ = "/".join(load_adapter.split("/")[:-1])+"/"+",".join(lang_adapter_names)
  print(fusion_path_)
  model.load_adapter_fusion(fusion_path_)
  model.load_adapter(load_adapter)
  #model.train_adapter_fusion_TA([task_name], lang_adapter_names)
  model.set_active_adapters([lang_adapter_names, task_name])
  adap_ids = torch.tensor([LANG2ID[it] for it in lang_adapter_names])
  #pdb.set_trace()
  model.to(args.device)
  output_test_results_file = os.path.join(args.output_dir, "test_results.txt")
  with open(output_test_results_file, "a") as result_writer:
    for lang in args.predict_langs.split(','):
      if not os.path.exists(os.path.join(args.data_dir, lang, 'test.{}'.format(args.model_name_or_path))):
        logger.info("Language {} does not exist".format(lang))
        continue
      adapter_weight = None
      if not args.adapter_weight:
        if (adapter_args.train_adapter or args.test_adapter) and not args.adapter_weight:
          pass
    
      else:
        if args.adapter_weight != "0":
            adapter_weight = [float(w) for w in args.adapter_weight.split(",")]
        pdb.set_trace()
        model.set_active_adapters([lang_adapter_names, [task_name]])
      train_fewshot_dataset = load_and_cache_examples(args, tokenizer, labels, pad_token_label_id, mode="train", lang=args.predict_langs, lang2id=lang2id, few_shot=args.few_shot)
      #pdb.set_trace()
      global_step, tr_loss = train(args,train_fewshot_dataset , model, tokenizer, labels, pad_token_label_id, lang_adapter_names, task_name, adap_ids, lang2id)
      result, predictions = evaluate(args, model, tokenizer, labels, pad_token_label_id, mode="test", lang=lang, adap_ids=adap_ids, lang2id=lang2id, adapter_weight=adapter_weight, task_name=task_name, calc_weight_step=args.calc_weight_step)

      # Save results
      if args.predict_save_prefix is not None:
        result_writer.write("=====================\nlanguage={}_{}\n".format(args.predict_save_prefix, lang))
      else:
        result_writer.write("=====================\nlanguage={}\n".format(lang))
      for key in sorted(result.keys()):
        result_writer.write("{} = {}\n".format(key, str(result[key])))
      # Save predictions
      if args.predict_save_prefix is not None:
        output_test_predictions_file = os.path.join(args.output_dir, "test_{}_{}_predictions.txt".format(args.predict_save_prefix, lang))
      else:
        output_test_predictions_file = os.path.join(args.output_dir, "test_{}_predictions.txt".format(lang))
      infile = os.path.join(args.data_dir, lang, "test.{}".format(args.model_name_or_path))
      idxfile = infile + '.idx'
      save_predictions(args, predictions, output_test_predictions_file, infile, idxfile)

def save_predictions(args, predictions, output_file, text_file, idx_file, output_word_prediction=False):
  # Save predictions
  with open(text_file, "r") as text_reader, open(idx_file, "r") as idx_reader:
    text = text_reader.readlines()
    index = idx_reader.readlines()
    assert len(text) == len(index)

  # Sanity check on the predictions
  with open(output_file, "w") as writer:
    example_id = 0
    prev_id = int(index[0])
    for line, idx in zip(text, index):
      if line == "" or line == "\n":
        example_id += 1
      else:
        cur_id = int(idx)
        output_line = '\n' if cur_id != prev_id else ''
        if output_word_prediction:
          output_line += line.split()[0] + '\t'
        output_line += predictions[example_id].pop(0) + '\n'
        writer.write(output_line)
        prev_id = cur_id

if __name__ == "__main__":
  main()
