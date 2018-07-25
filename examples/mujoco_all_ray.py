import argparse
import os

import ray
from ray import tune

from rllab.envs.normalized_env import normalize
from rllab.envs.mujoco.swimmer_env import SwimmerEnv
from rllab.envs.mujoco.ant_env import AntEnv
from rllab.envs.mujoco.humanoid_env import HumanoidEnv

from softlearning.algorithms import SAC
from softlearning.environments import (
    GymEnv,
    PusherEnv,
    ImagePusherEnv,
    MultiDirectionSwimmerEnv,
    MultiDirectionAntEnv,
    MultiDirectionHumanoidEnv,
    CrossMazeAntEnv)
from softlearning.environments.image_pusher import ImageForkReacherEnv

from softlearning.misc.utils import timestamp, set_seed
from softlearning.policies import (
    GaussianPolicy,
    LatentSpacePolicy,
    GMMPolicy,
    UniformPolicy)
from softlearning.samplers import SimpleSampler, ExtraPolicyInfoSampler
from softlearning.replay_pools import (
    SimpleReplayPool,
    ExtraPolicyInfoReplayPool)
from softlearning.value_functions import NNQFunction, NNVFunction
from softlearning.preprocessors import (
    FeedforwardNetPreprocessor,
    PREPROCESSOR_FUNCTIONS,
)
from examples.variants import parse_domain_and_task, get_variant_spec

ENVIRONMENTS = {
    'swimmer-gym': {
        'default': lambda: GymEnv('Swimmer-v1'),
    },
    'swimmer-rllab': {
        'default': SwimmerEnv,
        'multi-direction': MultiDirectionSwimmerEnv,
    },
    'ant-gym': {
        'default': lambda: GymEnv('Ant-v1'),
    },
    'ant-rllab': {
        'default': AntEnv,
        'multi-direction': MultiDirectionAntEnv,
        'cross-maze': CrossMazeAntEnv
    },
    'humanoid-gym': {
        'default': lambda: GymEnv('Humanoid-v1'),
        'standup': lambda: GymEnv('HumanoidStandup-v1')
    },
    'humanoid-rllab': {
        'default': HumanoidEnv,
        'multi-direction': MultiDirectionHumanoidEnv,
    },
    'hopper': {
        'default': lambda: GymEnv('Hopper-v1')
    },
    'half-cheetah': {
        'default': lambda: GymEnv('HalfCheetah-v1')
    },
    'walker': {
        'default': lambda: GymEnv('Walker2d-v1')
    },
    'pusher': {
        'default': PusherEnv,
        'image': ImagePusherEnv,
        'reach': ImageForkReacherEnv,
     },
}

DEFAULT_DOMAIN = DEFAULT_ENV = 'swimmer-rllab'
AVAILABLE_DOMAINS = set(ENVIRONMENTS.keys())
AVAILABLE_TASKS = set(y for x in ENVIRONMENTS.values() for y in x.keys())


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--domain',
                        type=str,
                        choices=AVAILABLE_DOMAINS,
                        default=None)
    parser.add_argument('--task',
                        type=str,
                        choices=AVAILABLE_TASKS,
                        default='default')
    parser.add_argument('--policy',
                        type=str,
                        nargs='+',
                        choices=('gaussian', 'gmm', 'lsp'),
                        default='gaussian')
    parser.add_argument('--env', type=str, default=DEFAULT_ENV)
    parser.add_argument('--exp_name', type=str, default=timestamp())
    parser.add_argument('--mode', type=str, default='local')
    parser.add_argument('--log_dir', type=str, default=None)
    args = parser.parse_args()

    return args


DEFAULT_SNAPSHOT_DIR = '~/ray/results'
DEFAULT_SNAPSHOT_MODE = 'none'
DEFAULT_SNAPSHOT_GAP = 1000


def setup_rllab_logger(variant):
    """Temporary setup for rllab logger previously handled by run_experiment.

    TODO.hartikainen: Remove this once we have gotten rid of rllab logger.
    """

    from rllab.misc import logger

    run_params = variant['run_params']

    ray_log_dir = os.getcwd()
    log_dir = os.path.join(ray_log_dir, 'rllab-logger')

    tabular_log_file = os.path.join(log_dir, 'progress.csv')
    text_log_file = os.path.join(log_dir, 'debug.log')
    variant_log_file = os.path.join(log_dir, 'variant.json')

    logger.log_variant(variant_log_file, variant)
    logger.add_text_output(text_log_file)
    logger.add_tabular_output(tabular_log_file)

    logger.set_snapshot_dir(log_dir)
    logger.set_snapshot_mode(
        run_params.get('snapshot_mode', DEFAULT_SNAPSHOT_MODE))
    logger.set_snapshot_gap(
        run_params.get('snapshot_gap', DEFAULT_SNAPSHOT_GAP))
    logger.set_log_tabular_only(False)

    # TODO.hartikainen: need to remove something, or push_prefix, pop_prefix?
    # logger.push_prefix("[%s] " % args.exp_name)


def run_experiment(variant, reporter):
    # Setup the rllab logger manually
    # TODO.hartikainen: We should change the logger to use some standard logger

    setup_rllab_logger(variant)
    set_seed(variant['run_params']['seed'])

    env_params = variant['env_params']
    policy_params = variant['policy_params']
    value_fn_params = variant['value_fn_params']
    preprocessor_params = variant['preprocessor_params']
    algorithm_params = variant['algorithm_params']
    replay_pool_params = variant['replay_pool_params']
    sampler_params = variant['sampler_params']

    task = variant['task']
    domain = variant['domain']

    # Unfortunately we have to do hack like this because ray logger fails
    # if our variant has parentheses.
    if 'image_size' in env_params:
        env_params['image_size'] = tuple(
            int(dim) for dim in env_params['image_size'].split('x'))

    preprocessor_kwargs = preprocessor_params.get('kwargs', {})
    if 'image_size' in preprocessor_kwargs:
        preprocessor_kwargs['image_size'] = tuple(
            int(dim) for dim in preprocessor_kwargs['image_size'].split('x'))
    if 'hidden_layer_sizes' in preprocessor_kwargs:
        preprocessor_kwargs['hidden_layer'] = tuple(
            int(dim) for dim in preprocessor_kwargs['hidden_layer'].split('x'))

    env = normalize(ENVIRONMENTS[domain][task](**env_params))

    if algorithm_params['store_extra_policy_info']:
        sampler = ExtraPolicyInfoSampler(**sampler_params)
        pool = ExtraPolicyInfoReplayPool(env_spec=env.spec, **replay_pool_params)
    else:
        sampler = SimpleSampler(**sampler_params)
        pool = SimpleReplayPool(env_spec=env.spec, **replay_pool_params)

    base_kwargs = dict(algorithm_params['base_kwargs'], sampler=sampler)

    M = value_fn_params['layer_size']
    qf1 = NNQFunction(env_spec=env.spec, hidden_layer_sizes=(M, M), name='qf1')
    qf2 = NNQFunction(env_spec=env.spec, hidden_layer_sizes=(M, M), name='qf2')
    vf = NNVFunction(env_spec=env.spec, hidden_layer_sizes=(M, M))
    initial_exploration_policy = UniformPolicy(env_spec=env.spec)

    if policy_params['type'] == 'gaussian':
        policy = GaussianPolicy(
            env_spec=env.spec,
            hidden_layer_sizes=(M, M),
            reparameterize=policy_params['reparameterize'],
            reg=1e-3,
        )
    elif policy_params['type'] == 'lsp':
        if preprocessor_params:
            preprocessor_fn = PREPROCESSOR_FUNCTIONS[
                preprocessor_params.get('function_name')]
            preprocessor = preprocessor_fn(
                *preprocessor_params.get('args', []),
                **preprocessor_params.get('kwargs', {}))
        else:
            preprocessor = None

        policy_s_t_layers = policy_params['s_t_layers']
        policy_s_t_units = policy_params['s_t_units']
        s_t_hidden_sizes = [policy_s_t_units] * policy_s_t_layers

        bijector_config = {
            'num_coupling_layers': policy_params['coupling_layers'],
            'translation_hidden_sizes': s_t_hidden_sizes,
            'scale_hidden_sizes': s_t_hidden_sizes,
        }

        policy = LatentSpacePolicy(
            env_spec=env.spec,
            squash=policy_params['squash'],
            bijector_config=bijector_config,
            reparameterize=policy_params['reparameterize'],
            q_function=qf1,
            observations_preprocessor=preprocessor)
    elif policy_params['type'] == 'gmm':
        assert not policy_params['reparameterize'], (
            "reparameterize should be False when using a GMMPolicy")
        policy = GMMPolicy(
            env_spec=env.spec,
            K=policy_params['K'],
            hidden_layer_sizes=(M, M),
            reparameterize=policy_params['reparameterize'],
            qf=qf1,
            reg=1e-3,
        )
    else:
        raise NotImplementedError(policy_params['type'])

    algorithm = SAC(
        base_kwargs=base_kwargs,
        env=env,
        policy=policy,
        initial_exploration_policy=initial_exploration_policy,
        pool=pool,
        qf1=qf1,
        qf2=qf2,
        vf=vf,
        lr=algorithm_params['lr'],
        target_entropy=algorithm_params['target_entropy'],
        reward_scale=algorithm_params['reward_scale'],
        discount=algorithm_params['discount'],
        tau=algorithm_params['tau'],
        reparameterize=policy_params['reparameterize'],
        target_update_interval=algorithm_params['target_update_interval'],
        action_prior=policy_params['action_prior'],
        store_extra_policy_info=algorithm_params['store_extra_policy_info'],
        save_full_state=False,
    )

    for epoch, mean_return in algorithm.train():
        reporter(timesteps_total=epoch, mean_accuracy=mean_return)


def main():
    args = parse_args()

    domain, task = args.domain, args.task
    if (not domain) or (not task):
        domain, task = parse_domain_and_task(args.env)

    tune.register_trainable('mujoco-runner', run_experiment)

    if args.mode == 'local':
        ray.init()
        local_dir_base = './data/ray/results'
    else:
        ray.init(redis_address=ray.services.get_node_ip_address() + ':6379')
        local_dir_base = '~/ray_results'

    local_dir = '{}/{}/{}'.format(local_dir_base, domain, task)

    variant_specs = []
    for policy in args.policy:
        variant_spec = get_variant_spec(
            domain=domain, task=task, policy=policy)
        variant_spec['run_params']['local_dir'] = local_dir
        variant_specs.append(variant_spec)

    tune.run_experiments({
        "{}-{}".format(args.exp_name, policy): {
            'run': 'mujoco-runner',
            'trial_resources': {'cpu': 16},
            'config': variant_spec,
            'local_dir': local_dir,
            'upload_dir': 'gs://sac-ray-test/ray/results'
        }
        for policy, variant_spec in zip(args.policy, variant_specs)
    })

if __name__ == '__main__':
    main()
