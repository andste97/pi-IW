"""
Example of pi-IW: interleaving planning and learning.
"""

import numpy as np
import tensorflow as tf
from planning_step import gridenvs_BASIC_features
from online_planning import softmax_Q_tree_policy


# Function that will be executed at each interaction with the environment
def get_observe_fn(use_dynamic_feats):
    if use_dynamic_feats:
        def observe(env, node):
            x = tf.constant(np.expand_dims(node.data["obs"], axis=0).astype(np.float32))
            logits, features = model(x, output_features=True)
            node.data["probs"] = tf.nn.softmax(logits).numpy().ravel()
            node.data["features"] = features.numpy().ravel()
    else:
        def observe(env, node):
            x = tf.constant(np.expand_dims(node.data["obs"], axis=0).astype(np.float32))
            logits = model(x)
            node.data["probs"] = tf.nn.softmax(logits).numpy().ravel()
            gridenvs_BASIC_features(env, node)  # compute BASIC features
    return observe


def planning_step(actor, planner, dataset, policy_fn, tree_budget, cache_subtree, discount_factor):
    nodes_before_planning = len(actor.tree)
    budget_fn = lambda: len(actor.tree) - nodes_before_planning == tree_budget
    planner.plan(tree=actor.tree,
                 successor_fn=actor.generate_successor,
                 stop_condition_fn=budget_fn,
                 policy_fn=policy_fn)
    tree_policy = softmax_Q_tree_policy(actor.tree, actor.tree.branching_factor, discount_factor, temp=0)
    a = sample_pmf(tree_policy)
    prev_root_data, current_root_data = actor.step(a, cache_subtree=cache_subtree)
    dataset.append({"observations": prev_root_data["obs"],
                    "target_policy": tree_policy})
    return current_root_data["r"], current_root_data["done"]

class TrainStats:
    def __init__(self):
        self.last_interactions = 0
        self.steps = 0
        self.episodes = 0

    def report(self, episode_rewards, total_interactions):
        self.episodes += 1
        self.steps += len(episode_rewards)
        print("Episode: %i."%self.episodes,
              "Reward: %.2f"%np.sum(episode_rewards),
              "Episode interactions %i."%(total_interactions-self.last_interactions),
              "Episode steps %i."%len(episode_rewards),
              "Total interactions %i."%total_interactions,
              "Total steps %i."%self.steps)
        self.last_interactions = total_interactions

if __name__ == "__main__":
    import gym
    from rollout_iw import RolloutIW
    from tree import TreeActor
    from supervised_policy import SupervisedPolicy, Mnih2013
    from utils import sample_pmf
    from experience_replay import ExperienceReplay
    import gridenvs.examples  # load simple envs


    # Compatibility with tensorflow 2.0
    tf.enable_eager_execution()
    tf.enable_resource_variables()


    # HYPERPARAMETERS
    seed = 0
    env_id = "GE_MazeKeyDoor-v0"
    use_dynamic_feats = False # otherwise BASIC features will be used
    tree_budget = 50
    discount_factor = 0.99
    cache_subtree = True
    batch_size = 32
    learning_rate = 0.0007
    replay_capacity = 1000
    replay_min_transitions = 100
    max_simulator_steps = 1000000
    regularization_factor = 0.001
    clip_grad_norm = 40
    rmsprop_decay = 0.99
    rmsprop_epsilon = 0.1


    # Set random seed
    np.random.seed(seed)
    tf.set_random_seed(seed)

    # Instead of env.step() and env.reset(), we'll use TreeActor helper class, which creates a tree and adds nodes to it
    env = gym.make(env_id)
    actor = TreeActor(env, observe_fn=get_observe_fn(use_dynamic_feats))
    planner = RolloutIW(branching_factor=env.action_space.n, ignore_cached_nodes=True)

    model = Mnih2013(num_logits=env.action_space.n, add_value=False)
    optimizer = tf.train.RMSPropOptimizer(learning_rate=learning_rate,
                                          decay=rmsprop_decay,
                                          epsilon=rmsprop_epsilon)
    learner = SupervisedPolicy(model, optimizer, regularization_factor=regularization_factor, use_graph=True)
    experience_replay = ExperienceReplay(capacity=replay_capacity)

    def network_policy(node, branching_factor):
        return node.data["probs"]

    # Initialize experience replay: run complete episodes until we exceed both batch_size and dataset_min_transitions
    print("Initializing experience replay", flush=True)
    train_stats = TrainStats()
    while len(experience_replay) < batch_size or len(experience_replay) < replay_min_transitions:
        episode_done = False
        actor.reset()
        episode_rewards = []
        while not episode_done:
            r, episode_done = planning_step(actor=actor,
                                            planner=planner,
                                            dataset=experience_replay,
                                            policy_fn=network_policy,
                                            tree_budget=tree_budget,
                                            cache_subtree=cache_subtree,
                                            discount_factor=discount_factor)
            episode_rewards.append(r)
        train_stats.report(episode_rewards, actor.nodes_generated)

    # Once initialized, interleave planning and learning steps
    print("\nInterleaving planning and learning steps.", flush=True)
    tree = actor.reset()
    aux_replay = ExperienceReplay()
    episode_rewards = []
    while actor.nodes_generated < max_simulator_steps:
        r, episode_done = planning_step(actor=actor,
                                        planner=planner,
                                        dataset=aux_replay,  # Add transitions to a separate episode buffer
                                        policy_fn=network_policy,
                                        tree_budget=tree_budget,
                                        cache_subtree=cache_subtree,
                                        discount_factor=discount_factor)

        # Add transitions to the experience replay buffer once the episode is over
        if episode_done:
            train_stats.report(episode_rewards, actor.nodes_generated)
            experience_replay.extend(aux_replay)
            aux_replay = ExperienceReplay()
            episode_rewards = []
            actor.reset()

        # Learning step
        batch = experience_replay.sample(batch_size)
        loss, _ = learner.train_step(tf.constant(batch["observations"], dtype=tf.float32),
                                     tf.constant(batch["target_policy"], dtype=tf.float32))
        # print(actor.tree.root.data["s"][0], "Simulator steps:", actor.nodes_generated, "\tPlanning steps:", steps_cnt, "\tLoss:", loss.numpy())
