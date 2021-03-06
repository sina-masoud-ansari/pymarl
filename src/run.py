import datetime
import os
import pprint
import time
import threading
import torch as th
from types import SimpleNamespace as SN
from utils.logging import Logger
from utils.timehelper import time_left, time_str
from os.path import dirname, abspath

from learners import REGISTRY as le_REGISTRY
from runners import REGISTRY as r_REGISTRY
from controllers import REGISTRY as mac_REGISTRY
from components.episode_buffer import ReplayBuffer
from components.transforms import OneHot

import pickle

def run(_run, _config, _log):

    # check args sanity
    _config = args_sanity_check(_config, _log)

    args = SN(**_config)
    args.device = "cuda" if args.use_cuda else "cpu"

    # setup loggers
    logger = Logger(_log)

    _log.info("Experiment Parameters:")
    experiment_params = pprint.pformat(_config,
                                       indent=4,
                                       width=1)
    _log.info("\n\n" + experiment_params + "\n")

    # configure tensorboard logger
    unique_token = "{}__{}".format(args.name, datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S"))
    args.unique_token = unique_token
    if args.use_tensorboard:
        tb_logs_direc = os.path.join(dirname(dirname(abspath(__file__))), "results", "tb_logs")
        tb_exp_direc = os.path.join(tb_logs_direc, "{}").format(unique_token)
        logger.setup_tb(tb_exp_direc)

    # sacred is on by default
    logger.setup_sacred(_run)

    # Run and train
    run_sequential(args=args, logger=logger)

    # Clean up after finishing
    print("Exiting Main")

    print("Stopping all threads")
    for t in threading.enumerate():
        if t.name != "MainThread":
            print("Thread {} is alive! Is daemon: {}".format(t.name, t.daemon))
            t.join(timeout=1)
            print("Thread joined")

    print("Exiting script")

    # Making sure framework really exits
    os._exit(os.EX_OK)

# TODO: need a way to save episodes that is separate from the buffer, i.e performs similar preprocessing
def evaluate_sequential(args, runner, buffer):

    for _ in range(args.test_nepisode):
        episode_batch = runner.run(test_mode=True)
        if args.save_episodes:
            buffer.insert_episode_batch(episode_batch)

    if args.save_replay:
        runner.save_replay()

    runner.close_env()

def run_sequential(args, logger):
    # Init runner so we can get env info
    runner = r_REGISTRY[args.runner](args=args, logger=logger)

    # Set up schemes and groups here
    env_info = runner.get_env_info()
    args.n_agents = env_info["n_agents"]
    args.n_actions = env_info["n_actions"]
    args.state_shape = env_info["state_shape"]
    args.episode_limit = env_info["episode_limit"]

    # Default/Base scheme
    scheme = {
        "state": {"vshape": env_info["state_shape"]},
        "obs": {"vshape": env_info["obs_shape"], "group": "agents"},
        "actions": {"vshape": (1,), "group": "agents", "dtype": th.long},
        "avail_actions": {"vshape": (env_info["n_actions"],), "group": "agents", "dtype": th.int},
        "reward": {"vshape": (1,)},
        "terminated": {"vshape": (1,), "dtype": th.uint8},
        "battle_won": {"vshape": (1,), "dtype": th.uint8},
    }
    groups = {
        "agents": args.n_agents
    }
    preprocess = {
        "actions": ("actions_onehot", [OneHot(out_dim=args.n_actions)])
    }

    buffer = ReplayBuffer(scheme, groups, args.buffer_size, env_info["episode_limit"] + 1,
                          preprocess=preprocess,
                          device="cpu" if args.buffer_cpu_only else args.device,
                          save_episodes=True if args.save_episodes else False,
                          episode_dir=args.episode_dir,
                          clear_existing_episodes=args.clear_existing_episodes)  # TODO maybe just pass args

    # Setup multiagent controller here
    mac = mac_REGISTRY[args.mac](buffer.scheme, groups, args)

    # Give runner the scheme
    runner.setup(scheme=scheme, groups=groups, preprocess=preprocess, mac=mac)

    # Learner
    learner = le_REGISTRY[args.learner](mac, buffer.scheme, logger, args)

    # Model learner
    model_learner = None
    model_buffer = None
    if args.model_learner:
        model_learner = le_REGISTRY[args.model_learner](mac, scheme, logger, args)
        model_buffer = ReplayBuffer(scheme, groups, args.model_buffer_size, buffer.max_seq_length,
                                    preprocess=preprocess,
                                    device="cpu" if args.buffer_cpu_only else args.device,
                                    save_episodes=False)

    if args.use_cuda:
        learner.cuda()
        if model_learner:
            model_learner.cuda()

    if args.checkpoint_path != "":
        if not os.path.isdir(args.checkpoint_path):
            logger.console_logger.info("Checkpoint directiory {} doesn't exist".format(args.checkpoint_path))
            return

        timestep_to_load = 0
        if args.rl_checkpoint:
            rl_timesteps = []

            # Go through all files in args.checkpoint_path
            for name in os.listdir(args.checkpoint_path):
                full_name = os.path.join(args.checkpoint_path, name)
                # Check if they are dirs the names of which are numbers
                name = name.replace('rl_', '')
                if os.path.isdir(full_name) and name.isdigit():
                    rl_timesteps.append(int(name))

            load_step = int(args.load_step.replace('rl_', '')) if isinstance(args.load_step, str) else args.load_step
            if load_step == 0:
                # choose the max timestep
                timestep_to_load = max(rl_timesteps)
            else:
                # choose the timestep closest to load_step
                timestep_to_load = min(rl_timesteps, key=lambda x: abs(x - load_step))
                model_path = os.path.join(args.checkpoint_path, f"rl_{timestep_to_load}")

        else:
            timesteps = []

            # Go through all files in args.checkpoint_path
            for name in os.listdir(args.checkpoint_path):
                full_name = os.path.join(args.checkpoint_path, name)
                # Check if they are dirs the names of which are numbers
                if os.path.isdir(full_name) and name.isdigit():
                    timesteps.append(int(name))

            if args.load_step == 0:
                # choose the max timestep
                timestep_to_load = max(timesteps)
            else:
                # choose the timestep closest to load_step
                timestep_to_load = min(timesteps, key=lambda x: abs(x - args.load_step))

                model_path = os.path.join(args.checkpoint_path, str(timestep_to_load))

        logger.console_logger.info("Loading model from {}".format(model_path))
        learner.load_models(model_path)
        runner.t_env = timestep_to_load

        if args.evaluate or args.save_replay:
            evaluate_sequential(args, runner, buffer)
            return

        # TODO checkpoints for model_learner

    # start training
    episode = 0
    last_test_T = -args.test_interval - 1
    last_log_T = 0
    model_save_time = 0

    start_time = time.time()
    last_time = start_time

    # new stuff
    collect_episodes = True
    collected_episodes = 0
    train_rl = False
    rl_iterations = 0
    model_trained = False
    n_model_trained = 0
    last_rl_T = 0
    rl_model_save_time = 0

    logger.console_logger.info("Beginning training for {} timesteps".format(args.t_max))
    while runner.t_env <= args.t_max:

        if model_learner:
            if collect_episodes:
                episode_batch = runner.run(test_mode=False)  # collect real episode to progress t_env
                print(f"Collecting {args.batch_size_run} episodes from REAL ENV using epsilon: {runner.mac.env_action_selector.epsilon:.2f}, t_env: {runner.t_env}, collected episodes: {collected_episodes}")
                buffer.insert_episode_batch(episode_batch)
                collected_episodes += args.batch_size_run

            n_collect = args.model_n_collect_episodes if model_trained else args.model_n_collect_episodes_initial
            if collected_episodes >= n_collect:
                print(f"Collected {collected_episodes} REAL episodes, training ENV model")
                # stop collection and train model
                collect_episodes = False
                collected_episodes = 0
                model_learner.train(buffer, runner.t_env, plot_test_results=False)
                model_trained = True
                n_model_trained += 1
                train_rl = True

                if args.model_rollout_before_rl:
                    print(f"Generating {args.model_rollouts} MODEL episodes")
                    rollouts = 0
                    rollout_batch_size = min(buffer.episodes_in_buffer, args.model_rollout_batch_size)
                    while rollouts < args.model_rollouts:
                        model_batch = model_learner.generate_batch(buffer, rollout_batch_size, rl_iterations)
                        model_buffer.insert_episode_batch(model_batch)
                        rollouts += rollout_batch_size

            if train_rl: # and model_buffer.can_sample(args.batch_size):

                # generate synthetic episodes under current policy
                if not args.model_rollout_before_rl:
                    print(f"Generating {args.model_rollouts} MODEL episodes")
                    rollout_batch_size = min(buffer.episodes_in_buffer, args.model_rollout_batch_size)
                    model_batch = model_learner.generate_batch(buffer, rollout_batch_size, rl_iterations)
                    model_buffer.insert_episode_batch(model_batch)

                if model_buffer.can_sample(args.batch_size):
                    for _ in range(args.model_rl_iterations_per_generated_sample):
                        episode_sample = model_buffer.sample(args.batch_size)

                        # truncate batch to only filled timesteps
                        max_ep_t = episode_sample.max_t_filled()
                        episode_sample = episode_sample[:, :max_ep_t]

                        if episode_sample.device != args.device:
                            episode_sample.to(args.device)

                        # train RL agent
                        learner.train(episode_sample, runner.t_env, rl_iterations)
                        rl_iterations += 1
                        print(f"Model RL iteration {rl_iterations}, t_env: {runner.t_env}")

            if not collect_episodes and rl_iterations > 0 and rl_iterations % args.model_update_interval == 0:
                if args.max_model_trained == 0 or args.max_model_trained and n_model_trained < args.max_model_trained:
                    print(f"Time to update model")
                    collect_episodes = True
                    train_rl = False

            # update stats
            model_learner.log_stats(runner.t_env)
            if (runner.t_env - last_log_T) >= args.log_interval:
                logger.log_stat("model_rl_iterations", rl_iterations, runner.t_env)
            if (rl_iterations > 0 and (rl_iterations - last_rl_T) /args.rl_test_interval >= 1.0):
                print(f"Logging rl stats")
                model_learner.log_rl_stats(rl_iterations)

        else:
            episode_batch = runner.run(test_mode=False)
            buffer.insert_episode_batch(episode_batch)
            if args.save_episodes and args.save_policy_outputs and args.runner == "episode":
                mac.save_policy_outputs()
            if buffer.can_sample(args.batch_size):
                for _ in range(args.batch_size_run):
                    episode_sample = buffer.sample(args.batch_size)

                    # Truncate batch to only filled timesteps
                    max_ep_t = episode_sample.max_t_filled()
                    episode_sample = episode_sample[:, :max_ep_t]

                    if episode_sample.device != args.device:
                        episode_sample.to(args.device)

                    learner.train(episode_sample, runner.t_env, episode)
                    rl_iterations += 1
                    print(f"RL iteration {rl_iterations}, t_env: {runner.t_env}")

        # Execute test runs once in a while
        n_test_runs = max(1, args.test_nepisode // runner.batch_size)
        if ((runner.t_env - last_test_T) / args.test_interval >= 1.0) or (rl_iterations > 0 and (rl_iterations - last_rl_T) /args.rl_test_interval >= 1.0):

            print("Running test cases")

            logger.console_logger.info("t_env: {} / {}".format(runner.t_env, args.t_max))
            logger.console_logger.info("Estimated time left: {}. Time passed: {}".format(
                time_left(last_time, last_test_T, runner.t_env, args.t_max), time_str(time.time() - start_time)))
            last_time = time.time()

            last_test_T = runner.t_env
            last_rl_T = rl_iterations
            runner.t_rl = rl_iterations

            for _ in range(n_test_runs):
                runner.run(test_mode=True)

            logger.print_recent_stats()

        if args.save_model and (runner.t_env - model_save_time >= args.save_model_interval or model_save_time == 0):
            model_save_time = runner.t_env
            save_path = os.path.join(args.local_results_path, "models", args.unique_token, str(runner.t_env))
            # "results/models/{}".format(unique_token)
            os.makedirs(save_path, exist_ok=True)
            logger.console_logger.info("Saving models to {}".format(save_path))

            # learner should handle saving/loading -- delegate actor save/load to mac,
            # use appropriate filenames to do critics, optimizer states
            learner.save_models(save_path)

        if args.save_model and model_trained and (rl_iterations == 0 or (rl_iterations - rl_model_save_time)/args.rl_save_model_interval >= 1.0):
            print(f"Saving at RL model iteration {rl_iterations}")
            rl_model_save_time = rl_iterations
            save_path = os.path.join(args.local_results_path, "models", args.unique_token, f"rl_{rl_iterations}")
            # "results/models/{}".format(unique_token)
            os.makedirs(save_path, exist_ok=True)
            logger.console_logger.info("Saving models to {}".format(save_path))

            # learner should handle saving/loading -- delegate actor save/load to mac,
            # use appropriate filenames to do critics, optimizer states
            learner.save_models(save_path)

        episode += args.batch_size_run

        if (runner.t_env - last_log_T) >= args.log_interval:
            logger.log_stat("rl_iterations", rl_iterations, runner.t_env)
            logger.log_stat("episode", episode, runner.t_env)
            logger.print_recent_stats()
            last_log_T = runner.t_env

    runner.close_env()
    logger.console_logger.info("Finished Training")

def save_buffer(buffer, filename, verbose=False):
    with open(filename, 'wb') as f:
        pickle.dump(buffer, f)
        if verbose:
            print(f"Saved buffer {filename}")

def args_sanity_check(config, _log):

    # set CUDA flags
    # config["use_cuda"] = True # Use cuda whenever possible!
    if config["use_cuda"] and not th.cuda.is_available():
        config["use_cuda"] = False
        _log.warning("CUDA flag use_cuda was switched OFF automatically because no CUDA devices are available!")

    if config["test_nepisode"] < config["batch_size_run"]:
        config["test_nepisode"] = config["batch_size_run"]
    else:
        config["test_nepisode"] = (config["test_nepisode"]//config["batch_size_run"]) * config["batch_size_run"]

    return config
