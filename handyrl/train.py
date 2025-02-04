# Copyright (c) 2020 DeNA Co., Ltd.
# Licensed under The MIT License [see LICENSE for details]

# training

import os
import time
import copy
import threading
import random
import bz2
import pickle
import warnings
import queue
from collections import deque

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.distributions as dist
import torch.optim as optim
import psutil

from .environment import prepare_env, make_env
from .util import map_r, bimap_r, trimap_r, rotate
from .model import to_torch, to_gpu, ModelWrapper
from .losses import compute_target
from .connection import MultiProcessJobExecutor
from .worker import WorkerCluster, WorkerServer

import wandb
import os

# Set up Weights & Biases
#run = wandb.init(project='hungry_geese_wandb')
#SAVE_FREQUENCY = 1


def make_batch(episodes, args):
    """Making training batch

    Args:
        episodes (Iterable): list of episodes
        args (dict): training configuration

    Returns:
        dict: PyTorch input and target tensors

    Note:
        Basic data shape is (B, T, P, ...) .
        (B is batch size, T is time length, P is player count)
    """

    obss, datum = [], []

    def replace_none(a, b):
        return a if a is not None else b

    for ep in episodes:
        moments_ = sum([pickle.loads(bz2.decompress(ms)) for ms in ep['moment']], [])
        moments = moments_[ep['start'] - ep['base']:ep['end'] - ep['base']]
        players = list(moments[0]['observation'].keys())
        if not args['turn_based_training']:  # solo training
            players = [random.choice(players)]

        # template for padding
        obs_zeros = map_r(moments[0]['observation'][moments[0]['turn'][0]], lambda o: np.zeros_like(o))
        amask_zeros = np.zeros_like(moments[0]['action_mask'][moments[0]['turn'][0]])

        # data that is changed by training configuration
        if args['turn_based_training'] and not args['observation']:
            obs = [[m['observation'][m['turn'][0]]] for m in moments]
            prob = np.array([[[m['selected_prob'][m['turn'][0]]]] for m in moments])
            act = np.array([[m['action'][m['turn'][0]]] for m in moments], dtype=np.int64)[..., np.newaxis]
            amask = np.array([[m['action_mask'][m['turn'][0]]] for m in moments])
        else:
            obs = [[replace_none(m['observation'][player], obs_zeros) for player in players] for m in moments]
            prob = np.array([[[replace_none(m['selected_prob'][player], 1.0)] for player in players] for m in moments])
            act = np.array([[replace_none(m['action'][player], 0) for player in players] for m in moments], dtype=np.int64)[..., np.newaxis]
            amask = np.array([[replace_none(m['action_mask'][player], amask_zeros + 1e32) for player in players] for m in moments])

        # reshape observation
        obs = rotate(rotate(obs))  # (T, P, ..., ...) -> (P, ..., T, ...) -> (..., T, P, ...)
        obs = bimap_r(obs_zeros, obs, lambda _, o: np.array(o))

        # datum that is not changed by training configuration
        v = np.array([[replace_none(m['value'][player], [0]) for player in players] for m in moments], dtype=np.float32).reshape(len(moments), len(players), -1)
        rew = np.array([[replace_none(m['reward'][player], [0]) for player in players] for m in moments], dtype=np.float32).reshape(len(moments), len(players), -1)
        ret = np.array([[replace_none(m['return'][player], [0]) for player in players] for m in moments], dtype=np.float32).reshape(len(moments), len(players), -1)
        oc = np.array([ep['outcome'][player] for player in players], dtype=np.float32).reshape(1, len(players), -1)

        emask = np.ones((len(moments), 1, 1), dtype=np.float32)  # episode mask
        tmask = np.array([[[m['selected_prob'][player] is not None] for player in players] for m in moments], dtype=np.float32)
        omask = np.array([[[m['observation'][player] is not None] for player in players] for m in moments], dtype=np.float32)

        progress = np.arange(ep['start'], ep['end'], dtype=np.float32)[..., np.newaxis] / ep['total']

        # pad each array if step length is short
        batch_steps = args['burn_in_steps'] + args['forward_steps']
        if len(tmask) < batch_steps:
            pad_len_b = args['burn_in_steps'] - (ep['train_start'] - ep['start'])
            pad_len_a = batch_steps - len(tmask) - pad_len_b
            obs = map_r(obs, lambda o: np.pad(o, [(pad_len_b, pad_len_a)] + [(0, 0)] * (len(o.shape) - 1), 'constant', constant_values=0))
            prob = np.pad(prob, [(pad_len_b, pad_len_a), (0, 0), (0, 0)], 'constant', constant_values=1)
            v = np.concatenate([np.pad(v, [(pad_len_b, 0), (0, 0), (0, 0)], 'constant', constant_values=0), np.tile(oc, [pad_len_a, 1, 1])])
            act = np.pad(act, [(pad_len_b, pad_len_a), (0, 0), (0, 0)], 'constant', constant_values=0)
            rew = np.pad(rew, [(pad_len_b, pad_len_a), (0, 0), (0, 0)], 'constant', constant_values=0)
            ret = np.pad(ret, [(pad_len_b, pad_len_a), (0, 0), (0, 0)], 'constant', constant_values=0)
            emask = np.pad(emask, [(pad_len_b, pad_len_a), (0, 0), (0, 0)], 'constant', constant_values=0)
            tmask = np.pad(tmask, [(pad_len_b, pad_len_a), (0, 0), (0, 0)], 'constant', constant_values=0)
            omask = np.pad(omask, [(pad_len_b, pad_len_a), (0, 0), (0, 0)], 'constant', constant_values=0)
            amask = np.pad(amask, [(pad_len_b, pad_len_a), (0, 0), (0, 0)], 'constant', constant_values=1e32)
            progress = np.pad(progress, [(pad_len_b, pad_len_a), (0, 0)], 'constant', constant_values=1)

        obss.append(obs)
        datum.append((prob, v, act, oc, rew, ret, emask, tmask, omask, amask, progress))

    obs = to_torch(bimap_r(obs_zeros, rotate(obss), lambda _, o: np.array(o)))
    prob, v, act, oc, rew, ret, emask, tmask, omask, amask, progress = [to_torch(np.array(val)) for val in zip(*datum)]

    return {
        'observation': obs,
        'selected_prob': prob,
        'value': v,
        'action': act, 'outcome': oc,
        'reward': rew, 'return': ret,
        'episode_mask': emask,
        'turn_mask': tmask, 'observation_mask': omask,
        'action_mask': amask,
        'progress': progress,
    }


def forward_prediction(model, hidden, batch, args):
    """Forward calculation via neural network

    Args:
        model (torch.nn.Module): neural network
        hidden: initial hidden state (..., B, P, ...)
        batch (dict): training batch (output of make_batch() function)

    Returns:
        tuple: batch outputs of neural network
    """

    observations = batch['observation']  # (..., B, T, P or 1, ...)
    batch_shape = batch['action'].size()[:3]  # (B, T, P or 1)

    if hidden is None:
        # feed-forward neural network
        obs = map_r(observations, lambda o: o.flatten(0, 2))  # (..., B * T * P or 1, ...)
        outputs = model(obs, None)
        outputs = map_r(outputs, lambda o: o.unflatten(0, batch_shape))  # (..., B, T, P or 1, ...)
    else:
        # sequential computation with RNN
        outputs = {}
        for t in range(batch_shape[1]):
            obs = map_r(observations, lambda o: o[:, t].flatten(0, 1))  # (..., B * P or 1, ...)
            omask_ = batch['observation_mask'][:, t]
            omask = map_r(hidden, lambda h: omask_.view(*h.size()[:2], *([1] * (h.dim() - 2))))
            hidden_ = bimap_r(hidden, omask, lambda h, m: h * m)  # (..., B, P, ...)
            if args['turn_based_training'] and not args['observation']:
                hidden_ = map_r(hidden_, lambda h: h.sum(1))  # (..., B * 1, ...)
            else:
                hidden_ = map_r(hidden_, lambda h: h.flatten(0, 1))  # (..., B * P, ...)
            if t < args['burn_in_steps']:
                model.eval()
                with torch.no_grad():
                    outputs_ = model(obs, hidden_)
            else:
                if not model.training:
                    model.train()
                outputs_ = model(obs, hidden_)
            outputs_ = map_r(outputs_, lambda o: o.unflatten(0, (batch_shape[0], batch_shape[2])))  # (..., B, P or 1, ...)
            for k, o in outputs_.items():
                if k == 'hidden':
                    next_hidden = o
                else:
                    outputs[k] = outputs.get(k, []) + [o]
            hidden = trimap_r(hidden, next_hidden, omask, lambda h, nh, m: h * (1 - m) + nh * m)
        outputs = {k: torch.stack(o, dim=1) for k, o in outputs.items() if o[0] is not None}

    for k, o in outputs.items():
        if k == 'policy':
            o = o.mul(batch['turn_mask'])
            if o.size(2) > 1 and batch_shape[2] == 1:  # turn-alternating batch
                o = o.sum(2, keepdim=True)  # gather turn player's policies
            outputs[k] = o - batch['action_mask']
        else:
            # mask valid target values and cumulative rewards
            outputs[k] = o.mul(batch['observation_mask'])

    return outputs


def compose_losses(outputs, log_selected_policies, total_advantages, targets, batch, args):
    """Caluculate loss value

    Returns:
        tuple: losses and statistic values and the number of training data
    """

    tmasks = batch['turn_mask']
    omasks = batch['observation_mask']

    losses = {}
    dcnt = tmasks.sum().item()

    losses['p'] = (-log_selected_policies * total_advantages).mul(tmasks).sum()
    if 'value' in outputs:
        losses['v'] = ((outputs['value'] - targets['value']) ** 2).mul(omasks).sum() / 2
    if 'return' in outputs:
        losses['r'] = F.smooth_l1_loss(outputs['return'], targets['return'], reduction='none').mul(omasks).sum()

    entropy = dist.Categorical(logits=outputs['policy']).entropy().mul(tmasks.sum(-1))
    losses['ent'] = entropy.sum()

    base_loss = losses['p'] + losses.get('v', 0) + losses.get('r', 0)
    entropy_loss = entropy.mul(1 - batch['progress'] * (1 - args['entropy_regularization_decay'])).sum() * -args['entropy_regularization']
    losses['total'] = base_loss + entropy_loss

    return losses, dcnt


def compute_loss(batch, model, hidden, args):
    outputs = forward_prediction(model, hidden, batch, args)
    if args['burn_in_steps'] > 0:
        batch = map_r(batch, lambda v: v[:, args['burn_in_steps']:] if v.size(1) > 1 else v)
        outputs = map_r(outputs, lambda v: v[:, args['burn_in_steps']:])

    actions = batch['action']
    emasks = batch['episode_mask']
    omasks = batch['observation_mask']
    value_target_masks, return_target_masks = omasks, omasks

    clip_rho_threshold, clip_c_threshold = 1.0, 1.0

    log_selected_b_policies = torch.log(torch.clamp(batch['selected_prob'], 1e-16, 1)) * emasks
    log_selected_t_policies = F.log_softmax(outputs['policy'], dim=-1).gather(-1, actions) * emasks

    # thresholds of importance sampling
    log_rhos = log_selected_t_policies.detach() - log_selected_b_policies
    rhos = torch.exp(log_rhos)
    clipped_rhos = torch.clamp(rhos, 0, clip_rho_threshold)
    cs = torch.clamp(rhos, 0, clip_c_threshold)
    outputs_nograd = {k: o.detach() for k, o in outputs.items()}

    if 'value' in outputs_nograd:
        values_nograd = outputs_nograd['value']
        if args['turn_based_training'] and values_nograd.size(2) == 2:  # two player zerosum game
            values_nograd_opponent = -torch.flip(values_nograd, dims=[2])
            omasks_opponent = torch.flip(omasks, dims=[2])
            values_nograd = (values_nograd * omasks + values_nograd_opponent * omasks_opponent) / (omasks + omasks_opponent + 1e-8)
            value_target_masks = torch.clamp(omasks + omasks_opponent, 0, 1)
        outputs_nograd['value'] = values_nograd * emasks + batch['outcome'] * (1 - emasks)

    # compute targets and advantage
    targets = {}
    advantages = {}

    value_args = outputs_nograd.get('value', None), batch['outcome'], None, args['lambda'], 1, clipped_rhos, cs, value_target_masks
    return_args = outputs_nograd.get('return', None), batch['return'], batch['reward'], args['lambda'], args['gamma'], clipped_rhos, cs, return_target_masks

    targets['value'], advantages['value'] = compute_target(args['value_target'], *value_args)
    targets['return'], advantages['return'] = compute_target(args['value_target'], *return_args)

    if args['policy_target'] != args['value_target']:
        _, advantages['value'] = compute_target(args['policy_target'], *value_args)
        _, advantages['return'] = compute_target(args['policy_target'], *return_args)

    # compute policy advantage
    total_advantages = clipped_rhos * sum(advantages.values())

    return compose_losses(outputs, log_selected_t_policies, total_advantages, targets, batch, args)


class Batcher:
    def __init__(self, args, episodes):
        self.args = args
        self.episodes = episodes
        self.executor = MultiProcessJobExecutor(self._worker, self._selector(), self.args['num_batchers'])

    def _selector(self):
        while True:
            yield [self.select_episode() for _ in range(self.args['batch_size'])]

    def _worker(self, conn, bid):
        print('started batcher %d' % bid)
        while True:
            episodes = conn.recv()
            batch = make_batch(episodes, self.args)
            conn.send(batch)
        print('finished batcher %d' % bid)

    def run(self):
        self.executor.start()

    def select_episode(self):
        while True:
            ep_count = min(len(self.episodes), self.args['maximum_episodes'])
            ep_idx = random.randrange(ep_count)
            accept_rate = 1 - (ep_count - 1 - ep_idx) / ep_count
            if random.random() >= accept_rate:
                continue
            try:
                ep = self.episodes[ep_idx]
                break
            except IndexError:
                continue
        turn_candidates = 1 + max(0, ep['steps'] - self.args['forward_steps'])  # change start turn by sequence length
        train_st = random.randrange(turn_candidates)
        st = max(0, train_st - self.args['burn_in_steps'])
        ed = min(train_st + self.args['forward_steps'], ep['steps'])
        st_block = st // self.args['compress_steps']
        ed_block = (ed - 1) // self.args['compress_steps'] + 1
        ep_minimum = {
            'args': ep['args'], 'outcome': ep['outcome'],
            'moment': ep['moment'][st_block:ed_block],
            'base': st_block * self.args['compress_steps'],
            'start': st, 'end': ed, 'train_start': train_st, 'total': ep['steps'],
        }
        return ep_minimum

    def batch(self):
        return self.executor.recv()


class Trainer:
    def __init__(self, args, model):
        self.episodes = deque()
        self.args = args
        self.gpu = torch.cuda.device_count()
        self.model = model
        self.default_lr = 3e-8
        self.data_cnt_ema = self.args['batch_size'] * self.args['forward_steps']
        self.params = list(self.model.parameters())
        lr = self.default_lr * self.data_cnt_ema
        self.optimizer = optim.Adam(self.params, lr=lr, weight_decay=1e-5) if len(self.params) > 0 else None
        self.steps = 0
        self.batcher = Batcher(self.args, self.episodes)
        self.update_flag = False
        self.update_queue = queue.Queue(maxsize=1)

        self.wrapped_model = ModelWrapper(self.model)
        self.trained_model = self.wrapped_model
        if self.gpu > 1:
            self.trained_model = nn.DataParallel(self.wrapped_model)

    def update(self):
        self.update_flag = True
        model, steps = self.update_queue.get()
        return model, steps

    def train(self):
        if self.optimizer is None:  # non-parametric model
            time.sleep(0.1)
            return self.model

        batch_cnt, data_cnt, loss_sum = 0, 0, {}
        if self.gpu > 0:
            self.trained_model.cuda()
        self.trained_model.train()

        while data_cnt == 0 or not self.update_flag:
            batch = self.batcher.batch()
            batch_size = batch['value'].size(0)
            player_count = batch['value'].size(2)
            hidden = self.wrapped_model.init_hidden([batch_size, player_count])
            if self.gpu > 0:
                batch = to_gpu(batch)
                hidden = to_gpu(hidden)

            losses, dcnt = compute_loss(batch, self.trained_model, hidden, self.args)

            self.optimizer.zero_grad()
            losses['total'].backward()
            nn.utils.clip_grad_norm_(self.params, 4.0)
            self.optimizer.step()

            batch_cnt += 1
            data_cnt += dcnt
            for k, l in losses.items():
                loss_sum[k] = loss_sum.get(k, 0.0) + l.item()

            self.steps += 1

        print('loss = %s' % ' '.join([k + ':' + '%.3f' % (l / data_cnt) for k, l in loss_sum.items()]))

        self.data_cnt_ema = self.data_cnt_ema * 0.8 + data_cnt / (1e-2 + batch_cnt) * 0.2
        for param_group in self.optimizer.param_groups:
            param_group['lr'] = self.default_lr * self.data_cnt_ema / (1 + self.steps * 1e-5)
        self.model.cpu()
        self.model.eval()
        return copy.deepcopy(self.model)

    def run(self):
        print('waiting training')
        while len(self.episodes) < self.args['minimum_episodes']:
            time.sleep(1)
        if self.optimizer is not None:
            self.batcher.run()
            print('started training')
        while True:
            model = self.train()
            self.update_flag = False
            self.update_queue.put((model, self.steps))
        print('finished training')

import numpy as np
from PIL import Image, ImageDraw
from kaggle_environments import make

from PIL import ImageColor

def create_board_image(board, cell_size=20, goose_colors=None, food_color='purple'):
    rows, cols = len(board), len(board[0])
    img = Image.new('RGB', (cols * cell_size, rows * cell_size), color='white')
    draw = ImageDraw.Draw(img)

    if goose_colors is None:
        goose_colors = ['red', 'green', 'blue', 'yellow']

    food_positions = []

    for row in range(rows):
        for col in range(cols):
            cell = board[row][col]
            if cell == -1:
                draw.rectangle([(col * cell_size, row * cell_size), ((col + 1) * cell_size, (row + 1) * cell_size)], fill='black')
            elif cell >= 0:
                base_color = ImageColor.getrgb(goose_colors[cell % len(goose_colors)])
                alpha = int(255 * (1 - (cell - int(cell))))
                color = tuple(int(c * alpha / 255) for c in base_color)
                draw.rectangle([(col * cell_size, row * cell_size), ((col + 1) * cell_size, (row + 1) * cell_size)], fill=color)
            elif cell == -2:
                food_positions.append((row, col))

    for pos in food_positions:
        row, col = pos
        draw.ellipse([(col * cell_size, row * cell_size), ((col + 1) * cell_size, (row + 1) * cell_size)], fill=food_color)

    return img



def state_to_board(state, config):
    rows, cols = config["rows"], config["columns"]
    board = np.full((rows, cols), -1, dtype=int)

    for index, goose in enumerate(state[0]["observation"]["geese"]):
        for pos in goose:
            row, col = pos // cols, pos % cols
            board[row][col] = index

    for pos in state[0]["observation"]["food"]:
        row, col = pos // cols, pos % cols
        board[row][col] = -2

    return board


def game_to_gif(frames, output_path, duration=500, loop=0):
    frames[0].save(output_path, format='GIF', append_images=frames[1:], save_all=True, duration=duration, loop=loop)

def play_and_create_gif(agent, output_path, cell_size=20, goose_colors=None, duration=500, loop=0):
    env = make("hungry_geese")
    env.reset(num_agents=4)
    frames = []

    while not env.done:
        actions = [agent(obs_to_agent_input(env.state[i], env.state[0]), env.state) for i in range(4)]
        env.step(actions)
        board = state_to_board(env.state, env.configuration)
        img = create_board_image(board, cell_size=cell_size, goose_colors=goose_colors)
        frames.append(img)


    game_to_gif(frames, output_path, duration=duration, loop=loop)


def obs_to_agent_input(state, full_state):
    geese = full_state["observation"]["geese"]
    food = full_state["observation"]["food"]
    index = state["observation"]["index"]
    remaining_time = state["observation"]["remainingOverageTime"]

    return {
        "geese": geese,
        "food": food,
        "index": index,
        "remainingOverageTime": remaining_time
    }

    
import importlib.util
import sys
import os

def load_agent_from_file(file_path):
    module_name = os.path.splitext(os.path.basename(file_path))[0]
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    agent_module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = agent_module
    spec.loader.exec_module(agent_module)
    return agent_module.agent

def create_gif_from_submission(submission_path, output_gif_path):
    # エージェントをファイルから読み込む
    agent = load_agent_from_file(submission_path)

    # ゲームをプレイしてGIFを生成
    play_and_create_gif(agent, output_gif_path)

import base64
import torch
import os
import pickle

def model_weights_to_base64(model_path):
    model = torch.load(model_path)
    model_data = pickle.dumps(model)
    encoded_weights = base64.b64encode(model_data).decode("utf-8")
    return encoded_weights

def create_submission_file(model_path, base_file_path, output_path):
    encoded_weights = model_weights_to_base64(model_path)
    with open(base_file_path, "r") as base_file:
        base_content = base_file.read()
    with open(output_path, "w") as f:
        f.write(f"PARAM = '{encoded_weights}'\n")
        f.write("\n")
        f.write(base_content)



class Learner:
    def __init__(self, args, net=None, remote=False):
        train_args = args['train_args']
        env_args = args['env_args']
        train_args['env'] = env_args
        args = train_args

        self.args = args
        random.seed(args['seed'])

        self.env = make_env(env_args)
        eval_modify_rate = (args['update_episodes'] ** 0.85) / args['update_episodes']
        self.eval_rate = max(args['eval_rate'], eval_modify_rate)
        self.shutdown_flag = False
        self.flags = set()
        self.win_rate = 0

        # trained datum
        self.model_epoch = self.args['restart_epoch']
        self.model = net if net is not None else self.env.net()
        if self.model_epoch > 0:
            self.model.load_state_dict(torch.load(self.model_path(self.model_epoch)), strict=False)

        # generated datum
        self.generation_results = {}
        self.num_episodes = 0
        self.num_returned_episodes = 0

        # evaluated datum
        self.results = {}
        self.results_per_opponent = {}
        self.num_results = 0

        # multiprocess or remote connection
        self.worker = WorkerServer(args) if remote else WorkerCluster(args)

        # thread connection
        self.trainer = Trainer(args, copy.deepcopy(self.model))

    def model_path(self, model_id):
        return os.path.join('models', str(model_id) + '.pth')

    def latest_model_path(self):
        return os.path.join('models', 'latest.pth')

    def update_model(self, model, steps):
        # get latest model and save it
        print('updated model(%d)' % steps)
        self.model_epoch += 1
        self.model = model
        os.makedirs('models', exist_ok=True)
        torch.save(model.state_dict(), self.model_path(self.model_epoch))
        torch.save(model.state_dict(), self.latest_model_path())

    def feed_episodes(self, episodes):
        # analyze generated episodes
        for episode in episodes:
            if episode is None:
                continue
            for p in episode['args']['player']:
                model_id = episode['args']['model_id'][p]
                outcome = episode['outcome'][p]
                n, r, r2 = self.generation_results.get(model_id, (0, 0, 0))
                self.generation_results[model_id] = n + 1, r + outcome, r2 + outcome ** 2
            self.num_returned_episodes += 1
            if self.num_returned_episodes % 100 == 0:
                print(self.num_returned_episodes, end=' ', flush=True)

        # store generated episodes
        self.trainer.episodes.extend([e for e in episodes if e is not None])

        mem_percent = psutil.virtual_memory().percent
        mem_ok = mem_percent <= 95
        maximum_episodes = self.args['maximum_episodes'] if mem_ok else int(len(self.trainer.episodes) * 95 / mem_percent)

        if not mem_ok and 'memory_over' not in self.flags:
            warnings.warn("memory usage %.1f%% with buffer size %d" % (mem_percent, len(self.trainer.episodes)))
            self.flags.add('memory_over')

        while len(self.trainer.episodes) > maximum_episodes:
            self.trainer.episodes.popleft()

    def feed_results(self, results):
        # store evaluation results
        for result in results:
            if result is None:
                continue
            for p in result['args']['player']:
                model_id = result['args']['model_id'][p]
                res = result['result'][p]
                n, r, r2 = self.results.get(model_id, (0, 0, 0))
                self.results[model_id] = n + 1, r + res, r2 + res ** 2

                if model_id not in self.results_per_opponent:
                    self.results_per_opponent[model_id] = {}
                opponent = result['opponent']
                n, r, r2 = self.results_per_opponent[model_id].get(opponent, (0, 0, 0))
                self.results_per_opponent[model_id][opponent] = n + 1, r + res, r2 + res ** 2

    def update(self):
        # call update to every component
        print()
        print('epoch %d' % self.model_epoch)

        if self.model_epoch not in self.results:
            print('win rate = Nan (0)')
        else:
            def output_wp(name, results):
                n, r, r2 = results
                mean = r / (n + 1e-6)
                name_tag = ' (%s)' % name if name != '' else ''
                print('win rate%s = %.3f (%.1f / %d)' % (name_tag, (mean + 1) / 2, (r + n) / 2, n))
                self.win_rate = (mean + 1) / 2
                
                #if (self.model_epoch % SAVE_FREQUENCY == 0)&(self.model_epoch>0):  # added this part
                if self.model_epoch>0:
                    current_epoch = self.model_epoch
                    current_model_path = self.latest_model_path()
                    # Log the artifact to Weights & Biases
                    artifact = wandb.Artifact(f'{current_epoch}.pt', type="model")
                    artifact.add_file(current_model_path, name=f'{current_epoch}.pt')
                    wandb.log_artifact(artifact) 
                    print('model sent to wandb as an artifact')

                    create_submission_file(current_model_path, './base.py', f'./agents/{current_epoch}.py') # .pt -> .py
                    create_gif_from_submission(f'./agents/{current_epoch}.py', f'./videos/{current_epoch}.gif') # generate gif anime
                    wandb.log(
                        {
                            'win_rate': (mean + 1) / 2, 
                            "visualized episode": wandb.Video(f'./videos/{current_epoch}.gif', fps=2, format="gif")
                        }
                    ) # send it to wandb

            keys = self.results_per_opponent[self.model_epoch]
            if len(self.args.get('eval', {}).get('opponent', [])) <= 1 and len(keys) <= 1:
                output_wp('', self.results[self.model_epoch])
            else:
                output_wp('total', self.results[self.model_epoch])
                for key in sorted(list(self.results_per_opponent[self.model_epoch])):
                    output_wp(key, self.results_per_opponent[self.model_epoch][key])

        if self.model_epoch not in self.generation_results:
            print('generation stats = Nan (0)')
        else:
            n, r, r2 = self.generation_results[self.model_epoch]
            mean = r / (n + 1e-6)
            std = (r2 / (n + 1e-6) - mean ** 2) ** 0.5
            print('generation stats = %.3f +- %.3f' % (mean, std))

        model, steps = self.trainer.update()
        if model is None:
            model = self.model
        self.update_model(model, steps)

        # clear flags
        self.flags = set()

    def server(self):
        # central conductor server
        # returns as list if getting multiple requests as list
        print('started server')
        prev_update_episodes = self.args['minimum_episodes']
        # no update call before storing minimum number of episodes + 1 epoch
        next_update_episodes = prev_update_episodes + self.args['update_episodes']

        while self.worker.connection_count() > 0 or not self.shutdown_flag:
            try:
                conn, (req, data) = self.worker.recv(timeout=0.3)
            except queue.Empty:
                continue

            multi_req = isinstance(data, list)
            if not multi_req:
                data = [data]
            send_data = []

            if req == 'args':
                if self.shutdown_flag:
                    send_data = [None] * len(data)
                else:
                    for _ in data:
                        args = {'model_id': {}}

                        # decide role
                        if self.num_results < self.eval_rate * self.num_episodes:
                            args['role'] = 'e'
                        else:
                            args['role'] = 'g'

                        if args['role'] == 'g':
                            # genatation configuration
                            args['player'] = self.env.players()
                            for p in self.env.players():
                                if p in args['player']:
                                    args['model_id'][p] = self.model_epoch
                                else:
                                    args['model_id'][p] = -1
                            self.num_episodes += 1

                        elif args['role'] == 'e':
                            # evaluation configuration
                            args['player'] = [self.env.players()[self.num_results % len(self.env.players())]]
                            for p in self.env.players():
                                if p in args['player']:
                                    args['model_id'][p] = self.model_epoch
                                else:
                                    args['model_id'][p] = -1
                            self.num_results += 1

                        send_data.append(args)

            elif req == 'episode':
                # report generated episodes
                self.feed_episodes(data)
                send_data = [None] * len(data)

            elif req == 'result':
                # report evaluation results
                self.feed_results(data)
                send_data = [None] * len(data)

            elif req == 'model':
                for model_id in data:
                    model = self.model
                    if model_id != self.model_epoch and model_id > 0:
                        try:
                            model = copy.deepcopy(self.model)
                            model.load_state_dict(torch.load(self.model_path(model_id)), strict=False)
                        except:
                            # return latest model if failed to load specified model
                            pass
                    send_data.append(pickle.dumps(model))

            if not multi_req and len(send_data) == 1:
                send_data = send_data[0]
            self.worker.send(conn, send_data)

            if self.num_returned_episodes >= next_update_episodes:
                prev_update_episodes = next_update_episodes
                next_update_episodes = prev_update_episodes + self.args['update_episodes']
                self.update()
                if self.args['epochs'] >= 0 and self.model_epoch >= self.args['epochs']:
                    self.shutdown_flag = True
        print('finished server')

    def run(self):
        # open training thread
        threading.Thread(target=self.trainer.run, daemon=True).start()
        # open generator, evaluator
        self.worker.run()
        self.server()
        


def train_main(args):
    prepare_env(args['env_args'])  # preparing environment is needed in stand-alone mode
    learner = Learner(args=args)
    learner.run()

def train_main2(env_args, train_args):
    run = wandb.init(config=train_args)
    args = {}
    args['env_args'] = env_args
    args['train_args'] = train_args
    prepare_env(args['env_args'])  # preparing environment is needed in stand-alone mode
    learner = Learner(args=args)
    learner.run()
    
    return learner.win_rate

def train_server_main(args):
    learner = Learner(args=args, remote=True)
    learner.run()
