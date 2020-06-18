import time
import torch
import torch.nn.functional as F
from modules.models.simple import SimPLeModel
import numpy as np
import random
from components.episode_buffer import EpisodeBatch
from functools import partial
from envs import REGISTRY as env_REGISTRY
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import os

class SimPLeLearner:
    def __init__(self, mac, scheme, logger, args):

        self.mac = mac
        self.args = args
        self.logger = logger
        self.device = self.args.device

        # used to get env metadata
        self.env = env_REGISTRY[self.args.env](**self.args.env_args)

        self.action_size = args.n_actions * args.n_agents
        self.state_size = args.state_shape - self.action_size if args.env_args["state_last_action"] else args.state_shape
        self.reward_size = scheme["reward"]["vshape"][0]
        self.term_size = scheme["terminated"]["vshape"][0]

        # state model
        self.state_model_input_size = self.state_size + self.action_size
        self.state_model_output_size = self.state_size + self.reward_size + self.term_size
        self.state_model = SimPLeModel(self.state_model_input_size, self.state_model_output_size, args.state_model_hidden_dim)

        # observation model
        self.obs_model_input_size = self.state_size + self.term_size
        if args.obs_model_include_last_action:
            self.obs_model_input_size += self.action_size

        self.agent_obs_size = scheme["obs"]["vshape"]
        self.obs_model_output_size = self.args.n_agents * (args.n_actions + self.agent_obs_size)
        self.obs_model = SimPLeModel(self.obs_model_input_size, self.obs_model_output_size, args.obs_model_hidden_dim)

        # meta
        self.training_iterations = 0

    def get_state_scheme(self, other_features=False, custom_features=False):

        nf_ally, nf_enemy, nf_other, nf_custom, scheme_ally, scheme_enemy, scheme_other, scheme_custom = self._build_state_scheme()

        scheme = {}
        for a in range(self.env.n_agents):
            for k, v in scheme_ally.items():
                idx = a * nf_ally + v
                name = f"ally_{a}_{k}"
                scheme[name] = idx

        for a in range(self.env.n_enemies):
            for k, v in scheme_enemy.items():
                idx = self.env.n_agents * nf_ally + a * nf_enemy + v
                name = f"enemy_{a}_{k}"
                scheme[name] = idx

        if other_features:
            for k, v in scheme_other.items():
                idx = self.env.n_agents * nf_ally + self.env.n_enemies * nf_enemy + v
                scheme[k] = idx

        if custom_features:
            n = len(scheme)
            for k, v in scheme_custom.items():
                idx = n + v
                scheme[k] = idx

        return scheme

    def _build_state_scheme(self):

        nf_ally = 4 + self.env.shield_bits_ally + self.env.unit_type_bits
        nf_enemy = 3 + self.env.shield_bits_enemy + self.env.unit_type_bits

        # allies
        ally_scheme = {"health": 0, "cooldown": 1, "x": 2, "y": 3}
        idx = 4
        if self.env.shield_bits_ally > 0:
            ally_scheme["ally_shield"] = idx; idx += 1
        if self.env.unit_type_bits > 0:
            ally_scheme["ally_type"] = idx; idx += 1

        # enemies
        enemy_scheme = {"health": 0, "x": 1, "y": 2}
        idx = 3
        if self.env.shield_bits_enemy > 0:
            enemy_scheme["shield"] = idx; idx += 1
        if self.env.unit_type_bits > 0:
            enemy_scheme["type"] = idx;

        # other
        nf_other = 0
        other_scheme = {}
        if self.env.state_last_action:
            nf_other = self.env.n_agents * self.env.n_actions
            for i in range(self.env.n_agents):
                for j in range(self.env.n_actions):
                    other_scheme[f"agent_{i}_action_{j}"] = i * self.env.n_actions + j
        if self.env.state_timestep_number:
            nf_other += 1
            other_scheme["timestep"] = len(other_scheme) + 1

        # custom
        nf_custom = 2
        custom_scheme = {"reward": 0, "term_signal": 1}

        return nf_ally, nf_enemy, nf_other, nf_custom, ally_scheme, enemy_scheme, other_scheme, custom_scheme

    def get_obs_scheme(self):
        move_feats_dim = np.product(self.env.get_obs_move_feats_size())
        enemy_feats_dim = np.product(self.env.get_obs_enemy_feats_size())
        ally_feats_dim = np.product(self.env.get_obs_ally_feats_size())
        own_feats_dim = np.product(self.env.get_obs_own_feats_size())

        scheme = {}
        fidx = -1
        for a in range(self.env.n_agents):

            # movement features
            for d in ["NORTH", "SOUTH", "EAST", "WEST"]:
                fname = f"agent_{a}_move_{d}"; fidx += 1; scheme[fname] = fidx

            if self.env.obs_pathing_grid:
                for i in range(self.env.n_obs_pathing):
                    fname = f"agent_{a}_pathing_{i}"; fidx += 1; scheme[fname] = fidx

            if self.env.obs_terrain_height:
                idx = fidx
                for i in range(idx, move_feats_dim):
                    fname = f"agent_{a}_terrain_{i}"; fidx += 1; scheme[fname] = fidx

                    # enemy features
            for e in range(self.env.n_enemies):
                fname = f"agent_{a}_enemy_{e}_in_range"; fidx += 1; scheme[fname] = fidx
                fname = f"agent_{a}_enemy_{e}_distance"; fidx += 1; scheme[fname] = fidx
                fname = f"agent_{a}_enemy_{e}_relative_x"; fidx += 1; scheme[fname] = fidx
                fname = f"agent_{a}_enemy_{e}_relative_y"; fidx += 1; scheme[fname] = fidx

                if self.env.obs_all_health:
                    fname = f"agent_{a}_enemy_{e}_health"; fidx += 1; scheme[fname] = fidx
                    if self.env.shield_bits_enemy > 0:
                        fname = f"agent_{a}_enemy_{e}_shield"; fidx += 1; scheme[fname] = fidx

                if self.env.unit_type_bits > 0:
                    for i in range(self.env.unit_type_bits):
                        fname = f"agent_{a}_enemy_{e}_type_{i}"; fidx += 1; scheme[fname] = fidx

            # ally features
            allies = [x for x in range(self.env.n_agents) if x != a]
            for y in allies:
                fname = f"agent_{a}_ally_{y}_visible"; fidx += 1; scheme[fname] = fidx
                fname = f"agent_{a}_ally_{y}_distance"; fidx += 1; scheme[fname] = fidx
                fname = f"agent_{a}_ally_{y}_relative_x"; fidx += 1; scheme[fname] = fidx
                fname = f"agent_{a}_ally_{y}_relative_y"; fidx += 1; scheme[fname] = fidx

                if self.env.obs_all_health:
                    fname = f"agent_{a}_ally_{y}_health"; fidx += 1; scheme[fname] = fidx
                    if self.env.shield_bits_ally > 0:
                        fname = f"agent_{a}_ally_{y}_shield"; fidx += 1; scheme[fname] = fidx

                if self.env.unit_type_bits > 0:
                    for i in range(self.env.unit_type_bits):
                        fname = f"agent_{a}_ally_{y}_type_{i}"; fidx += 1; scheme[fname] = fidx

                if self.env.obs_last_action:
                    fname = f"agent_{a}_ally_{y}_last_action"; fidx += 1; scheme[fname] = fidx

            # own features
            if self.env.obs_own_health:
                fname = f"agent_{a}_health"; fidx += 1; scheme[fname] = fidx
            if self.env.obs_timestep_number:
                fname = f"timestep"; fidx += 1; scheme[fname] = fidx

            if self.env.unit_type_bits > 0:
                for i in range(self.env.unit_type_bits):
                    fname = f"agent_{a}_type_{i}"; fidx += 1; scheme[fname] = fidx

        # available actions
        for i in range(self.env.n_agents):
            for j in range(self.env.n_actions):
                fname = f"agent_{i}_action_{j}_available"; fidx += 1; scheme[fname] = fidx

        return scheme

    def train_test_split(self, indices, test_ratio=0.1, shuffle=True):

        if shuffle:
            random.shuffle(indices)

        n = len(indices)
        n_test = max(1, int(test_ratio * n))
        train_indices = range(n - n_test)
        test_indices = range(len(train_indices), n)

        return train_indices, test_indices

    def get_training_episode_vars(self, ep):

        # per-agent quantities
        obs = ep["obs"][:, :-1, ...]  # observations
        aa = ep["avail_actions"][:, :-1, ...].float()  # available actions
        action = ep["actions_onehot"][:, :-1, ...]  # actions taken

        # flatten per-agent quantities
        nbatch, ntimesteps, _, _ = obs.size()
        obs = obs.view((nbatch, ntimesteps, -1))
        aa = aa.view((nbatch, ntimesteps, -1))
        action = action.view(nbatch, ntimesteps, -1)

        # state
        state = ep["state"][:, :-1, :]
        if self.args.env_args["state_last_action"]:
            state = state[:, :, :self.state_size]

        # reward
        reward = ep["reward"][:, :-1, :]

        # termination signal
        terminated = ep["terminated"][:, :-1].float()
        term_idx = torch.squeeze(terminated).max(0)[1].item()
        term_signal = torch.ones_like(terminated)
        term_signal[:, :term_idx, :] = 0

        # mask for active timesteps (except for term_signal which is always valid)
        mask = torch.ones_like(terminated)
        mask[:, term_idx + 1:, :] = 0

        obs *= mask
        aa *= mask
        action *= mask
        reward *= mask
        state *= mask

        return state, action, reward, term_signal, obs, aa, mask

    def get_batch(self, episodes, batch_size, use_mask=False):
        # TOOD: refactor to use list of ids passed to replay buffer
        bs = min(batch_size, len(episodes))
        batch = random.sample(episodes, bs)
        props = [torch.cat(t) for t in zip(*batch)]
        if use_mask:
            mask = props[-1]
            idx = int(mask.sum(1).max().item())
            props = [x[:, :idx, :] for x in props]
        return props

    def get_state_model_input_output(self, state, action, reward, term_signal, obs, aa, mask):

        # inputs
        s = state[:, :-1, :]  # state at time t
        a = action[:, :-1, :]  # joint action at time t

        # outputs
        ns = state[:, 1:, :]  # state at time t+1
        r = reward[:, :-1, :]  # reward at time t+1
        T = term_signal[:, :-1, :]  # terminated at t+1

        y = torch.cat((ns, r, T), dim=-1)
        return s, a, y

    def run_state_model(self, state, action, ht_ct=None):

        bs, steps, state_size = state.size()
        if not ht_ct:
            ht_ct = self.state_model.init_hidden(bs, self.device)
        yp = torch.zeros(bs, steps, self.state_model_output_size).to(self.device)

        st = state[:, 0, :]  # initial state
        for t in range(0, steps):
            at = action[:, t, :]
            xt = torch.cat((st, at), dim=-1)

            yt, ht_ct = self.state_model(xt, ht_ct)
            yp[:, t, :] = yt

            st = yt[:, :state_size]
        return yp, ht_ct

    def train_state_model(self, train_episodes, test_episodes):

        print(f"State Model Training ...")
        # model learning parameters
        lr = self.args.state_model_learning_rate
        grad_clip = self.args.state_model_grad_clip_norm
        batch_size = self.args.state_model_train_batch_size
        batch_size = min(batch_size, len(test_episodes))
        optimizer = torch.optim.Adam(self.state_model.parameters(), lr=lr)
        epochs = self.args.state_model_train_epochs
        log_epochs = self.args.state_model_train_log_epochs
        use_mask = False # learning a termination signal is easier with unmasked input
        train_err = []
        val_err = []

        # train state model
        for e in range(epochs):

            self.state_model.train()
            t_start = time.time()

            props = self.get_batch(train_episodes, batch_size, use_mask=use_mask)
            state, action, y = self.get_state_model_input_output(*props)
            yp, _ = self.run_state_model(state.to(self.device), action.to(self.device))
            optimizer.zero_grad()
            loss = F.mse_loss(yp, y.to(self.device))
            loss.backward()
            torch.nn.utils.clip_grad_norm_(self.state_model.parameters(), grad_clip)
            optimizer.step()

            train_err.append(loss.item())

            self.state_model.eval()
            with torch.no_grad():

                props = self.get_batch(test_episodes, batch_size, use_mask=use_mask)
                state, action, y = self.get_state_model_input_output(*props)
                yp, _ = self.run_state_model(state.to(self.device), action.to(self.device))
                val_err.append(F.mse_loss(yp, y.to(self.device)).item())

            if (e + 1) % log_epochs == 0:
                # report epoch losses
                train_err = np.array(train_err)
                val_err = np.array(val_err)
                t_epoch = time.time() - t_start
                print(f"epoch: {e + 1:<3}   train loss: mean {train_err.mean():.5f}, std: {train_err.std():.5f}   val loss: mean {val_err.mean():.5f}, std: {val_err.std():.5f}   time: {t_epoch:.2f} s")
                train_err = []
                val_err = []
                # self.logger.console_logger.info(f"Model training epoch {i}")

    def shift(self, t, n, pad=0):
        t = torch.roll(t, n, 1)
        t[:, :n, :] = pad
        return t

    def get_obs_model_input_output(self, state, action, reward, term_signal, obs, aa, mask):
        y = torch.cat((obs, aa), dim=-1)
        return state, action, term_signal, y

    def run_obs_model(self, state, last_action, term_signal, ht_ct=None):
        bs, steps, state_size = state.size()
        if not ht_ct:
            ht_ct = self.obs_model.init_hidden(bs, self.device)
        yp = torch.zeros(bs, steps, self.obs_model_output_size).to(self.device)

        for t in range(0, steps):

            st = state[:, t, :]
            at = last_action[:, t, :]
            tt = term_signal[:, t, :]

            if self.args.obs_model_include_last_action:
                xt = torch.cat((st, at, tt), dim=-1)
            else:
                xt = torch.cat((st, tt), dim=-1)

            yt, ht_ct = self.obs_model(xt, ht_ct)
            yp[:, t, :] = yt

        return yp, ht_ct

    def train_obs_model(self, train_episodes, test_episodes):
        # observation model training
        print(f"Observation Model Training ...")

        # model learning parameters
        lr = self.args.obs_model_learning_rate
        grad_clip = self.args.obs_model_grad_clip_norm
        batch_size = self.args.obs_model_train_batch_size
        batch_size = min(batch_size, len(test_episodes))
        optimizer = torch.optim.Adam(self.obs_model.parameters(), lr=lr)
        epochs = self.args.obs_model_train_epochs
        log_epochs = self.args.obs_model_train_log_epochs
        use_mask = self.args.obs_model_use_mask
        optimizer = torch.optim.Adam(self.obs_model.parameters(), lr=lr)
        train_err = []
        val_err = []

        self.state_model.eval()
        for e in range(epochs):
            t_start = time.time()
            # use state model and real actions to generate synthetic episodes from real starts
            with torch.no_grad():
                props = self.get_batch(train_episodes, batch_size, use_mask=use_mask)
                r_state, action, term_signal, y = self.get_obs_model_input_output(*props)
                m_state, _ = self.run_state_model(r_state.to(self.device), action.to(self.device))
                m_state = m_state[:, :-1, :r_state.size()[-1]]  # exclude reward and term_signal and final timestep

                # prepend first real state to model generated states
                s0 = torch.unsqueeze(r_state[:, 0, :], dim=1).to(self.device)
                m_state = torch.cat((s0, m_state), dim=1)

            # generate obs from states
            self.obs_model.train()
            yp, _ = self.run_obs_model(m_state, self.shift(action, 1).to(self.device), term_signal.to(self.device))

            # train obs model
            optimizer.zero_grad()
            loss = F.mse_loss(yp, y.to(self.device))
            loss.backward()
            torch.nn.utils.clip_grad_norm_(self.obs_model.parameters(), grad_clip)
            optimizer.step()
            train_err.append(loss.item())

            # validate obs model
            with torch.no_grad():
                props = self.get_batch(test_episodes, batch_size, use_mask=use_mask)
                r_state, action, term_signal, y = self.get_obs_model_input_output(*props)
                m_state, _ = self.run_state_model(r_state.to(self.device), action.to(self.device))
                m_state = m_state[:, :-1, :r_state.size()[-1]]  # exclude reward and term_signal and final timestep

                # prepend first real state to model generated states
                s0 = torch.unsqueeze(r_state[:, 0, :], dim=1).to(self.device)
                m_state = torch.cat((s0, m_state), dim=1)

                # generate obs from states
                self.obs_model.eval()
                yp, _ = self.run_obs_model(m_state, self.shift(action, 1).to(self.device), term_signal.to(self.device))
                val_err.append(F.mse_loss(yp, y.to(self.device)).item())

            if (e + 1) % log_epochs == 0:
                # report epoch losses
                train_err = np.array(train_err)
                val_err = np.array(val_err)
                t_epoch = time.time() - t_start
                print(
                    f"epoch: {e + 1:<3}   train loss: mean {train_err.mean():.5f}, std: {train_err.std():.5f}   val loss: mean {val_err.mean():.5f}, std: {val_err.std():.5f}   time: {t_epoch:.2f} s")
                train_err = []
                val_err = []

    def plot_state_model(self, test_episodes, plot_dir):

        batch_size = self.args.state_model_train_batch_size
        batch_size = min(batch_size, len(test_episodes))

        # get state model results
        self.state_model.eval()
        with torch.no_grad():
            props = self.get_batch(test_episodes, batch_size, use_mask=False)
            state, actions, y = self.get_state_model_input_output(*props)
            yp, _ = self.run_state_model(state.to(self.args.device), actions.to(self.args.device))

        y = y.to('cpu')
        yp = yp.to('cpu')

        idx = random.choice(range(batch_size))
        scheme = self.get_state_scheme(custom_features=True)

        fig, ax = plt.subplots(len(scheme), figsize=(5, 5 * len(scheme)))
        for k, v in scheme.items():
            i = scheme[k]
            ax[i].plot(y[idx, :, i], label='actual')
            ax[i].plot(yp[idx, :, i], label='predicted')
            ax[i].set_title(k)
        plt.savefig(os.path.join(plot_dir, f"state_{self.training_iterations}.png"))
        plt.close()

    def plot_obs_model(self, test_episodes, plot_dir):

        batch_size = self.args.state_model_train_batch_size
        batch_size = min(batch_size, len(test_episodes))

        # get obs model results
        self.state_model.eval()
        with torch.no_grad():
            props = self.get_batch(test_episodes, batch_size, use_mask=False)
            r_state, action, term_signal, y = self.get_obs_model_input_output(*props)
            m_state, _ = self.run_state_model(r_state.to(self.device), action.to(self.device))
            m_state = m_state[:, :-1, :r_state.size()[-1]]  # exclude reward and term_signal and final timestep

            # prepend first real state to model generated states
            s0 = torch.unsqueeze(r_state[:, 0, :], dim=1).to(self.device)
            m_state = torch.cat((s0, m_state), dim=1)

            self.obs_model.eval()
            yp, _ = self.run_obs_model(m_state, self.shift(action, 1).to(self.device), term_signal.to(self.device))


        y = y.to('cpu')
        yp = yp.to('cpu')

        idx = random.choice(range(batch_size))
        scheme = self.get_obs_scheme()

        fig, ax = plt.subplots(len(scheme), figsize=(5, 5 * len(scheme)))
        for k, v in scheme.items():
            i = scheme[k]
            ax[i].plot(y[idx, :, i], label='actual')
            ax[i].plot(yp[idx, :, i], label='predicted')
            ax[i].set_title(k)
        plt.savefig(os.path.join(plot_dir, f"obs_{self.training_iterations}.png"))
        plt.close()

    def train(self, buffer, plot_test_results=False, plot_dir="plots"):

        print(f"Training with {buffer.episodes_in_buffer} episodes")

        # generate training and test episode indices
        indices = list(range(0, buffer.episodes_in_buffer))
        train_indices, test_indices = self.train_test_split(indices, test_ratio=self.args.model_training_test_ratio, shuffle=True)

        # extract episodes
        train_episodes = [self.get_training_episode_vars(buffer[i]) for i in train_indices]
        test_episodes = [self.get_training_episode_vars(buffer[i]) for i in train_indices]

        self.train_state_model(train_episodes, test_episodes)
        self.train_obs_model(train_episodes, test_episodes)
        self.training_iterations += 1

        if plot_test_results:
            self.plot_state_model(test_episodes, plot_dir)
            self.plot_obs_model(test_episodes, plot_dir)

    def generate_batch(self, buffer, t_env):

        self.state_model.eval()
        self.obs_model.eval()

        # sample real starts from the replay buffer
        batch_size = min(buffer.episodes_in_buffer, self.args.model_rollout_batch_size)
        episodes = buffer.sample(batch_size)
        #self.logger.console_logger.info(f"Generating {batch_size} model based episodes")

        # create new episode batch for generated episodes
        scheme = buffer.scheme.copy()
        scheme.pop("filled", None)  # buffer scheme excluding filled key
        batch = partial(EpisodeBatch, scheme, buffer.groups, batch_size, buffer.max_seq_length,
                        preprocess=buffer.preprocess, device=self.device)()

        # construct observation model input
        state = episodes["state"][:, 0, :self.state_size].unsqueeze(1).to(self.device)
        term_signal = episodes["terminated"][:, 0, :].unsqueeze(1).float().to(self.device)
        terminated = (term_signal > 0)
        action = torch.zeros_like(episodes["actions_onehot"][:, 0, :].view(batch_size, 1, -1)).to(self.device)
        obs_size = self.args.n_agents * self.agent_obs_size

        # initialise hidden states
        o_ht_ct = None # obs model hidden states
        s_ht_ct = None # state model hidden states
        self.mac.init_hidden(batch_size=batch_size)

        # generate episode sequence
        for t in range(batch.max_seq_length):

            # update active episodes
            active_episodes = [i for i, finished in enumerate(terminated.squeeze()) if not finished]
            if all(terminated):
                break

            # generate obs from state
            output, o_ht_ct = self.run_obs_model(state, action, term_signal, ht_ct=o_ht_ct)

            batch_state = state
            if self.args.env_args["state_last_action"]:
                batch_state = torch.cat((state, action), dim=-1)
            obs = output[:, 0, :obs_size].view(batch_size, 1, self.args.n_agents, self.agent_obs_size)
            avail_actions = output[:, 0, obs_size:].view(batch_size, 1, self.args.n_agents, self.args.n_actions)

            # threshold avail_actions
            threshold = 0.2
            avail_actions = (avail_actions > threshold).float()
            #avail_actions[avail_actions < 0] = 0 # clip, we assume the distribution approaches the correct one over time
            avail_actions[:, :, :, 1] = 1 # stop action is always available
            #avail_actions[:, :, :, :] = 1  # all actions are available

            pre_transition_data = {
                "state": batch_state[active_episodes],
                "avail_actions": avail_actions[active_episodes],
                "obs": obs[active_episodes]
            }
            batch.update(pre_transition_data, bs=active_episodes, ts=t)

            # generate actions following current policy
            action = self.mac.select_actions(batch, t_ep=t, t_env=t_env, bs=active_episodes).unsqueeze(1)
            batch.update({"actions": action}, bs=active_episodes, ts=t) # this will generate actions_onehot
            action = batch["actions_onehot"][:, -1, ...].view(batch_size, 1, -1)  # latest action

            # generate next state, reward and termination signal
            output, s_ht_ct = self.run_state_model(state, action, ht_ct=s_ht_ct)
            state = output[:, :, :self.state_size]; idx = self.state_size
            reward = output[:, :, idx:idx + self.reward_size]; idx += self.reward_size
            term_signal = output[:, :, idx:idx + self.term_size]

            # generate termination mask
            threshold = 0.9
            terminated = (term_signal > threshold)

            post_transition_data = {
                "reward": reward[active_episodes],
                "terminated": terminated[active_episodes]
            }
            batch.update(post_transition_data, ts=t, bs=active_episodes)

        return batch

    def cuda(self):
        self.state_model.cuda()
        self.obs_model.cuda()