import atexit
import contextlib
import gc
import json
import multiprocessing
import os
import shutil
import subprocess
import sys
import tempfile
import termios
import time
import traceback
import tty
import warnings
from argparse import ArgumentParser
from multiprocessing import Process, Queue
from multiprocessing.pool import ThreadPool
from pathlib import Path
from pprint import pprint

import gym
import nle.nethack as nh
import numpy as np

from autoascend import agent as agent_lib
from autoascend.visualization import visualizer
from autoascend.utils import plot_dashboard


def fork_with_nethack_env(env):
    tmpdir = tempfile.mkdtemp(prefix='nlecopy_')
    shutil.copytree(env.env._vardir, tmpdir, dirs_exist_ok=True)
    env.env._tempdir = None  # it has to be done before the fork to avoid removing the same directory two times
    gc.collect()

    pid = os.fork()

    env.env._tempdir = tempfile.TemporaryDirectory(prefix='nlefork_')
    shutil.copytree(tmpdir, env.env._tempdir.name, dirs_exist_ok=True)
    env.env._vardir = env.env._tempdir.name
    os.chdir(env.env._vardir)
    return pid


def reload_agent(base_path=str(Path(__file__).parent.absolute())):
    global visualize, agent_lib
    visualize = agent_lib = None
    modules_to_remove = []
    for k, m in sys.modules.items():
        if hasattr(m, '__file__') and m.__file__ and m.__file__.startswith(base_path):
            modules_to_remove.append(k)
    del m

    gc.collect()
    while modules_to_remove:
        for k in modules_to_remove:
            assert sys.getrefcount(sys.modules[k]) >= 2
            if sys.getrefcount(sys.modules[k]) == 2:
                sys.modules.pop(k)
                modules_to_remove.remove(k)
                gc.collect()
                break
        else:
            assert 0, ('cannot unload agent library',
                       {k: sys.getrefcount(sys.modules[k]) for k in modules_to_remove})


class ReloadAgent(KeyboardInterrupt):
    # it inherits from KeyboardInterrupt as the agent doesn't catch that exception
    pass


class EnvWrapper:
    def __init__(self, env, to_skip=0, visualizer_args=dict(enable=False),
                 step_limit=None, agent_args={}, interactive=False):
        self.env = env
        self.agent_args = agent_args
        self.interactive = interactive
        self.to_skip = to_skip
        self.step_limit = step_limit
        self.visualizer = None
        if visualizer_args['enable']:
            visualizer_args.pop('enable')
            self.visualizer = visualizer.Visualizer(self, **visualizer_args)
        self.last_observation = None
        self.agent = None

        self.draw_walkable = False
        self.draw_seen = False
        self.draw_shop = False

        self.is_done = False

    def _init_agent(self):
        self.agent = agent_lib.Agent(self, **self.agent_args)

    def main(self):
        self.reset()
        while 1:
            try:
                self._init_agent()
                self.agent.main()
                break
            except ReloadAgent:
                pass
            finally:
                self.render()

            self.agent = None
            reload_agent()

    def reset(self):
        obs = self.env.reset()
        self.score = 0
        self.step_count = 0
        self.end_reason = ''
        self.last_observation = obs
        self.is_done = False

        if self.agent is not None:
            self.render()

        agent_lib.G.assert_map(obs['glyphs'], obs['chars'])

        blstats = agent_lib.BLStats(*obs['blstats'])
        assert obs['chars'][blstats.y, blstats.x] == ord('@')

        return obs

    def fork(self):
        fork_again = True
        while fork_again:
            pid = fork_with_nethack_env(self.env)
            if pid != 0:
                # parent
                print('freezing parent')
                while 1:
                    try:
                        os.waitpid(pid, 0)
                        break
                    except KeyboardInterrupt:
                        pass
                self.visualizer.force_next_frame()
                self.visualizer.render()
                while 1:
                    try:
                        fork_again = input('fork again [yn]: ')
                        if fork_again == 'y':
                            fork_again = True
                            break
                        elif fork_again == 'n':
                            fork_again = False
                            break
                    except KeyboardInterrupt:
                        pass

                termios.tcgetattr(sys.stdin)
                tty.setcbreak(sys.stdin.fileno())
            else:
                # child
                atexit.unregister(multiprocessing.util._exit_function)
                self.visualizer.force_next_frame()
                self.visualizer.render()
                break

    def render(self, force=False):
        if self.visualizer is not None:
            with self.debug_tiles(self.agent.current_level().walkable, color=(0, 255, 0, 128)) \
                    if self.draw_walkable else contextlib.suppress():
                with self.debug_tiles(~self.agent.current_level().seen, color=(255, 0, 0, 128)) \
                        if self.draw_seen else contextlib.suppress():
                    with self.debug_tiles(self.agent.current_level().shop, color=(0, 0, 255, 64)) \
                            if self.draw_shop else contextlib.suppress():
                        with self.debug_tiles(self.agent.current_level().shop_interior, color=(0, 0, 255, 64)) \
                                if self.draw_shop else contextlib.suppress():
                            with self.debug_tiles((self.last_observation['specials'] & nh.MG_OBJPILE) > 0,
                                                  color=(0, 255, 255, 128)):
                                with self.debug_tiles([self.agent.cursor_pos],
                                                      color=(255, 255, 255, 128)):
                                    if force:
                                        self.visualizer.force_next_frame()
                                    rendered = self.visualizer.render()

            if not force and (not self.interactive or not rendered):
                return

            if self.agent is not None:
                print('Message:', self.agent.message)
                print('Pop-up :', self.agent.popup)
            print()
            if self.agent is not None and hasattr(self.agent, 'blstats'):
                print(agent_lib.BLStats(*self.last_observation['blstats']))
                print(f'Carrying: {self.agent.inventory.items.total_weight} / {self.agent.character.carrying_capacity}')
                print('Character:', self.agent.character)
            print('Misc :', self.last_observation['misc'])
            print('Score:', self.score)
            print('Steps:', self.env._steps)
            print('Turns:', self.env._turns)
            print('Seed :', self.env.get_seeds())
            print('Items below me :', self.agent.inventory.items_below_me)
            print('Engraving below me:', self.agent.inventory.engraving_below_me)
            print()
            print(self.agent.inventory.items)
            print('-' * 20)

            self.env.render()
            print('-' * 20)
            print()

    def print_help(self):
        scene_glyphs = set(self.env.last_observation[0].reshape(-1))
        obj_classes = {getattr(nh, x): x for x in dir(nh) if x.endswith('_CLASS')}
        glyph_classes = sorted((getattr(nh, x), x) for x in dir(nh) if x.endswith('_OFF'))

        texts = []
        for i in range(nh.MAX_GLYPH):
            desc = ''
            if glyph_classes and i == glyph_classes[0][0]:
                cls = glyph_classes.pop(0)[1]

            if nh.glyph_is_monster(i):
                desc = f': "{nh.permonst(nh.glyph_to_mon(i)).mname}"'

            if nh.glyph_is_normal_object(i):
                obj = nh.objclass(nh.glyph_to_obj(i))
                appearance = nh.OBJ_DESCR(obj) or nh.OBJ_NAME(obj)
                oclass = ord(obj.oc_class)
                desc = f': {obj_classes[oclass]}: "{appearance}"'

            desc2 = 'Labels: '
            if i in agent_lib.G.INV_DICT:
                desc2 += ','.join(agent_lib.G.INV_DICT[i])

            if i in scene_glyphs:
                pos = (self.env.last_observation[0].reshape(-1) == i).nonzero()[0]
                count = len(pos)
                pos = pos[0]
                char = bytes([self.env.last_observation[1].reshape(-1)[pos]])
                texts.append((-count, f'{" " if i in agent_lib.G.INV_DICT else "U"} Glyph {i:4d} -> '
                                      f'Char: {char} Count: {count:4d} '
                                      f'Type: {cls.replace("_OFF", ""):11s} {desc:30s} '
                                      f'{agent_lib.ALL.find(i) if agent_lib.ALL.find(i) is not None else "":20} '
                                      f'{desc2}'))
        for _, t in sorted(texts):
            print(t)

    def get_action(self):
        while 1:
            key = os.read(sys.stdin.fileno(), 5)

            if key == b'\x1bOP':  # F1
                self.draw_walkable = not self.draw_walkable
                self.visualizer.force_next_frame()
                self.render()
                continue
            elif key == b'\x1bOQ':  # F2
                self.draw_seen = not self.draw_seen
                self.visualizer.force_next_frame()
                self.render()
                continue

            elif key == b'\x1bOR':  # F3
                self.draw_shop = not self.draw_shop
                self.visualizer.force_next_frame()
                self.render()
                continue

            if key == b'\x1bOS':  # F4
                raise ReloadAgent()

            if key == b'\x1b[15~':  # F5
                self.fork()
                continue

            elif key == b'\x1b[3~':  # Delete
                self.to_skip = 16
                return None

            if len(key) != 1:
                print('wrong key', key)
                continue
            key = key[0]
            if key == 10:
                key = 13

            if key == 63:  # '?"
                self.print_help()
                continue
            elif key == 127:  # Backspace
                self.visualizer.force_next_frame()
                return None
            else:
                actions = [a for a in self.env._actions if int(a) == key]
                assert len(actions) < 2
                if len(actions) == 0:
                    print('wrong key', key)
                    continue

                action = actions[0]
                return action

    def step(self, agent_action):
        if self.visualizer is not None and self.visualizer.video_writer is None:
            self.visualizer.step(self.last_observation, repr(chr(int(agent_action))))

            if self.interactive and self.to_skip <= 1:
                self.visualizer.force_next_frame()
            self.render()

            if self.interactive:
                print()
                print('agent_action:', agent_action, repr(chr(int(agent_action))))
                print()

            if self.to_skip > 0:
                self.to_skip -= 1
                action = None
            else:
                action = self.get_action()

            if action is None:
                action = agent_action

            if self.interactive:
                print('action:', action)
                print()
        else:
            if self.visualizer is not None:
                self.visualizer.step(self.last_observation, repr(chr(int(agent_action))))
            action = agent_action

        obs, reward, done, info = self.env.step(self.env._actions.index(action))
        self.score += reward
        self.step_count += 1
        # if not done:
        #     agent_lib.G.assert_map(obs['glyphs'], obs['chars'])

        # uncomment to debug measure up to assumed median
        # if self.score >= 7000:
        #     done = True
        #     self.end_reason = 'quit after median'
        if done:
            if self.visualizer is not None:
                self.visualizer.step(self.last_observation, repr(chr(int(agent_action))))

            end_reason = bytes(obs['tty_chars'].reshape(-1)).decode().replace('You made the top ten list!', '').split()
            if end_reason[7].startswith('Agent'):
                self.score = int(end_reason[6])
                end_reason = ' '.join(end_reason[8:-2])
            else:
                assert self.score == 0, end_reason
                end_reason = ' '.join(end_reason[7:-2])
            first_sentence = end_reason.split('.')[0].split()
            self.end_reason = info['end_status'].name + ': ' + \
                              (' '.join(first_sentence[:first_sentence.index('in')]) + '. ' + \
                               '.'.join(end_reason.split('.')[1:]).strip()).strip()
        if self.step_limit is not None and self.step_count == self.step_limit + 1:
            self.end_reason = self.end_reason or 'steplimit'
            done = True
        elif self.step_limit is not None and self.step_count > self.step_limit + 1:
            assert 0

        self.last_observation = obs

        if done:
            self.is_done = True
            if self.visualizer is not None:
                self.render()
            if self.interactive:
                print('Summary:')
                pprint(self.get_summary())

        return obs, reward, done, info

    def debug_tiles(self, *args, **kwargs):
        if self.visualizer is not None:
            return self.visualizer.debug_tiles(*args, **kwargs)
        return contextlib.suppress()

    def debug_log(self, txt, color=(255, 255, 255)):
        if self.visualizer is not None:
            return self.visualizer.debug_log(txt, color)
        return contextlib.suppress()

    def get_summary(self):
        return {
            'score': self.score,
            'steps': self.env._steps,
            'turns': self.agent.blstats.time,
            'level_num': len(self.agent.levels),
            'experience_level': self.agent.blstats.experience_level,
            'milestone': self.agent.global_logic.milestone,
            'panic_num': len(self.agent.all_panics),
            'character': str(self.agent.character).split()[0],
            'end_reason': self.end_reason,
            'seed': self.env.get_seeds(),
            **self.agent.stats_logger.get_stats_dict(),
        }


def prepare_env(args, seed, step_limit=None):
    seed += args.seed

    if args.role:
        while 1:
            env = gym.make('NetHackChallenge-v0')
            env.seed(seed, seed)
            obs = env.reset()
            blstats = agent_lib.BLStats(*obs['blstats'])
            character_glyph = obs['glyphs'][blstats.y, blstats.x]
            if any([nh.permonst(nh.glyph_to_mon(character_glyph)).mname.startswith(role) for role in args.role]):
                break
            seed += 10 ** 9
            env.close()

    if args.visualize_ends is not None:
        assert args.mode == 'simulate'
        args.skip_to = 2 ** 32

    visualize_with_simulate = args.visualize_ends is not None or args.output_video_dir is not None
    visualizer_args = dict(enable=args.mode == 'run' or visualize_with_simulate,
                           start_visualize=args.visualize_ends[seed] if args.visualize_ends is not None else None,
                           show=args.mode == 'run',
                           output_dir=Path('/tmp/vis/') / str(seed),
                           frame_skipping=None if not visualize_with_simulate else 1,
                           output_video_path=(args.output_video_dir / f'{seed}.mp4'
                                              if args.output_video_dir is not None else None))
    env = EnvWrapper(gym.make('NetHackChallenge-v0', no_progress_timeout=1000),
                     to_skip=args.skip_to, visualizer_args=visualizer_args,
                     agent_args=dict(panic_on_errors=args.panic_on_errors,
                                     verbose=args.mode == 'run'),
                     interactive=args.mode == 'run')
    env.env.seed(seed, seed)
    return env


def single_simulation(args, seed_offset, timeout=720):
    start_time = time.time()
    env = prepare_env(args, seed_offset)

    try:
        if timeout is not None:
            with ThreadPool(1) as pool:
                pool.apply_async(env.main).get(timeout)
        else:
            env.main()
    except multiprocessing.context.TimeoutError:
        env.end_reason = f'timeout'
    except BaseException as e:
        env.end_reason = f'exception: {"".join(traceback.format_exception(None, e, e.__traceback__))}'
        print(f'Seed {env.env.get_seeds()}, step {env.step_count}:', env.end_reason)

    end_time = time.time()
    summary = env.get_summary()
    summary['duration'] = end_time - start_time

    if args.visualize_ends is not None:
        env.visualizer.save_end_history()

    if env.visualizer is not None and env.visualizer.video_writer is not None:
        env.visualizer.video_writer.close()
    env.env.close()

    return summary


def run_single_interactive_game(args):
    termios.tcgetattr(sys.stdin)
    tty.setcbreak(sys.stdin.fileno())
    try:
        summary = single_simulation(args, 0, timeout=None)
        pprint(summary)
    finally:
        os.system('stty sane')


def run_profiling(args):
    if args.profiler == 'cProfile':
        import cProfile, pstats
    elif args.profiler == 'pyinstrument':
        from pyinstrument import Profiler
    elif args.profiler == 'none':
        pass
    else:
        assert 0

    if args.profiler == 'cProfile':
        pr = cProfile.Profile()
    elif args.profiler == 'pyinstrument':
        profiler = Profiler()
    elif args.profiler == 'none':
        pass
    else:
        assert 0

    if args.profiler == 'cProfile':
        pr.enable()
    elif args.profiler == 'pyinstrument':
        profiler.start()
    elif args.profiler == 'none':
        pass
    else:
        assert 0

    start_time = time.time()
    res = []
    for i in range(args.episodes):
        print(f'starting {i + 1} game...')
        res.append(single_simulation(args, i, timeout=None))
    duration = time.time() - start_time

    if args.profiler == 'cProfile':
        pr.disable()
    elif args.profiler == 'pyinstrument':
        session = profiler.stop()
    elif args.profiler == 'none':
        pass
    else:
        assert 0

    print()
    print('turns_per_second :', sum([r['turns'] for r in res]) / duration)
    print('steps_per_second :', sum([r['steps'] for r in res]) / duration)
    print('episodes_per_hour:', len(res) / duration * 3600)
    print()

    if args.profiler == 'cProfile':
        stats = pstats.Stats(pr).sort_stats(pstats.SortKey.CUMULATIVE)
        stats.print_stats(30)
        stats = pstats.Stats(pr).sort_stats(pstats.SortKey.TIME)
        stats.print_stats(30)
        stats.dump_stats('/tmp/nethack_stats.profile')

        subprocess.run('gprof2dot -f pstats /tmp/nethack_stats.profile -o /tmp/calling_graph.dot'.split())
        subprocess.run('xdot /tmp/calling_graph.dot'.split())
    elif args.profiler == 'pyinstrument':
        frame_records = session.frame_records

        new_records = []
        for record in frame_records:
            ret_frames = []
            for frame in record[0][1:][::-1]:
                func, module, line = frame.split('\0')
                if func in ['f', 'f2', 'run', 'wrapper']:
                    continue
                ret_frames.append(frame)
                if module.endswith('agent.py') and func in ['step', 'preempt', 'call_update_functions']:
                    break
            ret_frames.append(record[0][0])
            new_records.append((ret_frames[::-1], record[1] / session.duration * 100))
        session.frame_records = new_records
        session.start_call_stack = [session.start_call_stack[0]]

        print('Cumulative time:')
        profiler._last_session = session
        print(profiler.output_text(unicode=True, color=True))

        new_records = []
        for record in frame_records:
            ret_frames = []
            for frame in record[0][1:][::-1]:
                func, module, line = frame.split('\0')
                ret_frames.append(frame)
                if str(Path(module).absolute()).startswith(str(Path(__file__).parent.absolute())):
                    break
            ret_frames.append(record[0][0])
            new_records.append((ret_frames[::-1], record[1] / session.duration * 100))
        session.frame_records = new_records
        session.start_call_stack = [session.start_call_stack[0]]

        print('Total time:')
        profiler._last_session = session
        print(profiler.output_text(unicode=True, color=True, show_all=True))
    elif args.profiler == 'none':
        pass
    else:
        assert 0


def run_simulations(args):
    import ray
    ray.init(address='auto')

    start_time = time.time()
    plot_queue = Queue()

    def plot_thread_func():
        from matplotlib import pyplot as plt
        import seaborn as sns

        warnings.filterwarnings('ignore')
        sns.set()

        fig = plt.figure()
        plt.show(block=False)
        while 1:
            res = None
            try:
                while 1:
                    res = plot_queue.get(block=False)
            except:
                plt.pause(0.5)
                if res is None:
                    continue

            fig.clear()
            plot_dashboard(fig, res)
            fig.tight_layout()
            plt.show(block=False)

    if not args.no_plot:
        plt_process = Process(target=plot_thread_func)
        plt_process.start()

    refs = []

    @ray.remote(num_gpus=1 / 4 if args.with_gpu else 0)
    def remote_simulation(args, seed_offset, timeout=500):
        # I think there is some nondeterminism in nle environment when playing
        # multiple episodes (maybe bones?). That should do the trick
        q = Queue()

        if args.output_video_dir is not None:
            timeout = 4 * 24 * 60 * 60

        def sim():
            q.put(single_simulation(args, seed_offset, timeout=timeout))

        try:
            p = Process(target=sim, daemon=False)
            p.start()
            return q.get()
        finally:
            p.terminate()
            p.join()

        # uncomment to debug why join doesn't work properly
        # from multiprocessing.pool import ThreadPool
        # with ThreadPool(1) as thrpool:
        #     def fun():
        #         import time
        #         while True:
        #             time.sleep(1)
        #             print(p.pid, p.is_alive(), p.exitcode, p)
        #     thrpool.apply_async(fun)
        # p.join(timeout=timeout + 1)
        # assert not q.empty()

    try:
        with Path('/workspace/nh_sim.json').open('r') as f:
            all_res = json.load(f)
        print('Continue running: ', (len(all_res['seed'])))
    except FileNotFoundError:
        all_res = {}

    done_seeds = set()
    if 'seed' in all_res:
        done_seeds = set(s[0] for s in all_res['seed'])

    # remove runs finished with exceptions if rerunning with --panic-on-errors
    if args.panic_on_errors and all_res:
        idx_to_repeat = set()
        for i, (seed, reason) in enumerate(zip(all_res['seed'], all_res['end_reason'])):
            if reason.startswith('exception'):
                idx_to_repeat.add(i)
                done_seeds.remove(seed[0])
        print('Repeating idx:', idx_to_repeat)
        for k, v in all_res.items():
            all_res[k] = [v for i, v in enumerate(v) if i not in idx_to_repeat]

    print('skipping seeds', done_seeds)
    for seed_offset in range(args.episodes):
        seed = args.seed + seed_offset
        if seed in done_seeds:
            continue
        if args.seeds and seed not in args.seeds:
            continue
        if args.visualize_ends is None or seed_offset in [k % 10 ** 9 for k in args.visualize_ends]:
            refs.append(remote_simulation.remote(args, seed_offset))

    count = len(done_seeds)
    initial_count = count
    for handle in refs:
        ref, refs = ray.wait(refs, num_returns=1, timeout=None)
        single_res = ray.get(ref[0])

        if not all_res:
            all_res = {key: [] for key in single_res}
        assert all_res.keys() == single_res.keys()

        count += 1
        for k, v in single_res.items():
            all_res[k].append(v if not hasattr(v, 'item') else v.item())

        plot_queue.put(all_res)

        total_duration = time.time() - start_time

        median_score_std = np.std([np.median(np.random.choice(all_res["score"],
                                                              size=max(1, len(all_res["score"]) // 2)))
                                   for _ in range(1024)])

        text = []
        text.append(f'count                         : {count}')
        text.append(f'time_per_simulation           : {np.mean(all_res["duration"])}')
        text.append(f'simulations_per_hour          : {3600 / np.mean(all_res["duration"])}')
        text.append(f'simulations_per_hour(multi)   : {3600 * (count - initial_count) / total_duration}')
        text.append(f'time_per_turn                 : {np.sum(all_res["duration"]) / np.sum(all_res["turns"])}')
        text.append(f'turns_per_second              : {np.sum(all_res["turns"]) / np.sum(all_res["duration"])}')
        text.append(f'turns_per_second(multi)       : {np.sum(all_res["turns"]) / total_duration}')
        text.append(f'panic_num_per_game(median)    : {np.median(all_res["panic_num"])}')
        text.append(f'panic_num_per_game(mean)      : {np.sum(all_res["panic_num"]) / count}')
        text.append(f'score_median                  : {np.median(all_res["score"]):.1f} +/- '
                    f'{median_score_std:.1f}')
        text.append(f'score_mean                    : {np.mean(all_res["score"]):.1f} +/- '
                    f'{np.std(all_res["score"]) / (len(all_res["score"]) ** 0.5):.1f}')
        text.append(f'score_05-95                   : {np.quantile(all_res["score"], 0.05)} '
                    f'{np.quantile(all_res["score"], 0.95)}')
        text.append(f'score_25-75                   : {np.quantile(all_res["score"], 0.25)} '
                    f'{np.quantile(all_res["score"], 0.75)}')
        text.append(f'exceptions                    : '
                    f'{sum([r.startswith("exception:") for r in all_res["end_reason"]])}')
        text.append(f'steplimit                     : '
                    f'{sum([r.startswith("steplimit") or r.startswith("ABORT") for r in all_res["end_reason"]])}')
        text.append(f'timeout                       : '
                    f'{sum([r.startswith("timeout") for r in all_res["end_reason"]])}')
        print('\n'.join(text) + '\n')

        if args.visualize_ends is None:
            with Path('/workspace/nh_sim.json').open('w') as f:
                json.dump(all_res, f)

    print('DONE!')
    ray.shutdown()


def parse_args():
    parser = ArgumentParser()
    parser.add_argument('mode', choices=('simulate', 'run', 'profile'))
    parser.add_argument('--seed', type=int, help='Starting random seed')
    parser.add_argument('--seeds', nargs="*", type=int, help='Run only these specific seeds (only relevant in simulate mode)')
    parser.add_argument('--skip-to', type=int, default=0)
    parser.add_argument('-n', '--episodes', type=int, default=1)
    parser.add_argument('--role', choices=('arc', 'bar', 'cav', 'hea', 'kni',
                                           'mon', 'pri', 'ran', 'rog', 'sam',
                                           'tou', 'val', 'wiz'),
                        action='append')
    parser.add_argument('--panic-on-errors', action='store_true')
    parser.add_argument('--no-plot', action='store_true')
    parser.add_argument('--visualize-ends', type=Path, default=None,
                        help='Path to json file with dict: seed -> visualization_start_step')
    parser.add_argument('--output-video-dir', type=Path, default=None,
                        help="Episode visualization video directory -- valid only with 'simulate' mode")
    parser.add_argument('--profiler', choices=('cProfile', 'pyinstrument', 'none'), default='pyinstrument')
    parser.add_argument('--with-gpu', action='store_true')

    args = parser.parse_args()
    if args.seed is None:
        args.seed = np.random.randint(0, 2 ** 30)

    if args.visualize_ends is not None:
        with args.visualize_ends.open('r') as f:
            args.visualize_ends = {int(k): int(v) for k, v in json.load(f).items()}

    if args.output_video_dir is not None:
        assert args.mode == 'simulate', "Video output only valid in 'simulate' mode"

    print('ARGS:', args)
    return args


def main():
    args = parse_args()
    if args.mode == 'simulate':
        run_simulations(args)
    elif args.mode == 'profile':
        run_profiling(args)
    elif args.mode == 'run':
        run_single_interactive_game(args)


if __name__ == '__main__':
    main()