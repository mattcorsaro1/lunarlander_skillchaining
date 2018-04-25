# Matt Corsaro
# Brown University CS 2951X Final Project
# Skill chaining for continuous Lunar Lander
# Original DQN code from:
# https://gist.github.com/heerad/d2b92c2f3a83b5e4be395546c17b274c#file-dqn-lunarlander-v2-py
import numpy as np
import gym
from gym import wrappers
import tensorflow as tf

import time
import datetime
import os
from os import path
import sys
import random
from collections import deque

import argparse

def main():

    parser = argparse.ArgumentParser(description = "Lunar Lander")
    parser.add_argument('--visualize', dest='visualize', action='store_true')
    parser.add_argument('--no-visualize', dest='visualize', action='store_false')
    parser.set_defaults(feature=False)
    args = parser.parse_args()

    # DQN Params
    gamma = 0.99
    # Hidden layer sizes
    h1 = 200
    h2 = 200
    h3 = 200
    lr = 5e-5
    # decay per episode
    lr_decay = 1
    l2_reg = 1e-6
    dropout = 0
    num_episodes = 1000
    # gym cuts off after 1000, anyway
    max_steps_ep = 1000
    update_slow_target_every = 100
    train_every = 1
    replay_memory_capacity = int(1e6)
    minibatch_size = 1024
    epsilon_start = 1.0
    epsilon_end = 0.05
    epsilon_decay_length = 10000
    epsilon_decay_exp = 0.98

    # Skill chain params
    # How long to wait before adding new option?
    steps_per_opt = num_episodes/10
    # don't execute after creating, off-policy learning
    gestation = 10
    # Stop adding options after this timestep
    add_opt_cutoff = num_episodes/2

    # game parameters
    env = gym.make("LunarLander-v2")
    state_dim = np.prod(np.array(env.observation_space.shape))
    n_actions = env.action_space.n

    # set seeds to 0
    env.seed(0)
    np.random.seed(0)

    ####################################################################################################################
    ## Tensorflow

    tf.reset_default_graph()

    # placeholders
    state_ph = tf.placeholder(dtype=tf.float32, shape=[None,state_dim]) # input to Q network
    next_state_ph = tf.placeholder(dtype=tf.float32, shape=[None,state_dim]) # input to slow target network
    action_ph = tf.placeholder(dtype=tf.int32, shape=[None]) # action indices (indices of Q network output)
    reward_ph = tf.placeholder(dtype=tf.float32, shape=[None]) # rewards (go into target computation)
    is_not_terminal_ph = tf.placeholder(dtype=tf.float32, shape=[None]) # indicators (go into target computation)
    is_training_ph = tf.placeholder(dtype=tf.bool, shape=()) # for dropout

    episode_reward = tf.Variable(0.)
    tf.summary.scalar("Episode Reward", episode_reward)
    r_summary_placeholder = tf.placeholder("float")
    update_ep_reward = episode_reward.assign(r_summary_placeholder)

    # episode counter
    episodes = tf.Variable(0.0, trainable=False, name='episodes')
    episode_inc_op = episodes.assign_add(1)

    # will use this to initialize both Q network and slowly-changing target network with same structure
    def generate_network(s, trainable, reuse):
        hidden = tf.layers.dense(s, h1, activation = tf.nn.relu, trainable = trainable, name = 'dense', reuse = reuse)
        hidden_drop = tf.layers.dropout(hidden, rate = dropout, training = trainable & is_training_ph)
        hidden_2 = tf.layers.dense(hidden_drop, h2, activation = tf.nn.relu, trainable = trainable, name = 'dense_1', \
            reuse = reuse)
        hidden_drop_2 = tf.layers.dropout(hidden_2, rate = dropout, training = trainable & is_training_ph)
        hidden_3 = tf.layers.dense(hidden_drop_2, h3, activation = tf.nn.relu, trainable = trainable, name = 'dense_2',\
            reuse = reuse)
        hidden_drop_3 = tf.layers.dropout(hidden_3, rate = dropout, training = trainable & is_training_ph)
        action_values = tf.squeeze(tf.layers.dense(hidden_drop_3, n_actions, trainable = trainable, name = 'dense_3', \
            reuse = reuse))
        return action_values

    with tf.variable_scope('q_network') as scope:
        # Q network applied to state_ph
        q_action_values = generate_network(state_ph, trainable = True, reuse = False)
        # Q network applied to next_state_ph (for double Q learning)
        q_action_values_next = tf.stop_gradient(generate_network(next_state_ph, trainable = False, reuse = True))

    # slow target network
    with tf.variable_scope('slow_target_network', reuse=False):
        # use stop_gradient to treat the output values as constant targets when doing backprop
        slow_target_action_values = tf.stop_gradient(generate_network(next_state_ph, trainable = False, reuse = False))

    # isolate vars for each network
    q_network_vars = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope='q_network')
    slow_target_network_vars = tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES, scope='slow_target_network')

    # update values for slowly-changing target network to match current critic network
    update_slow_target_ops = []
    for i, slow_target_var in enumerate(slow_target_network_vars):
        update_slow_target_op = slow_target_var.assign(q_network_vars[i])
        update_slow_target_ops.append(update_slow_target_op)

    update_slow_target_op = tf.group(*update_slow_target_ops, name='update_slow_target')

    targets = reward_ph + is_not_terminal_ph * gamma * \
        tf.gather_nd(slow_target_action_values, tf.stack((tf.range(minibatch_size), \
            tf.cast(tf.argmax(q_action_values_next, axis=1), tf.int32)), axis=1))

    # Estimated Q values for (s,a) from experience replay
    estim_taken_action_vales = tf.gather_nd(q_action_values, tf.stack((tf.range(minibatch_size), action_ph), axis=1))

    # loss function (with regularization)
    loss = tf.reduce_mean(tf.square(targets - estim_taken_action_vales))
    for var in q_network_vars:
        if not 'bias' in var.name:
            loss += l2_reg * 0.5 * tf.nn.l2_loss(var)

    # optimizer
    train_op = tf.train.AdamOptimizer(lr*lr_decay**episodes).minimize(loss)

    board_name = datetime.datetime.fromtimestamp(time.time()).strftime('board_%Y_%m_%d_%H_%M_%S')
    class Option:
        def __init__(self, n):
            self.sess = tf.Session()
            self.sess.run(tf.global_variables_initializer())

            self.writer = tf.summary.FileWriter(board_name + '_' + str(n))
            self.writer.add_graph(self.sess.graph)

            self.experience = deque(maxlen=replay_memory_capacity)

        def writeReward(self, r, ep):
            self.sess.run(update_ep_reward, feed_dict={r_summary_placeholder: r})
            summary_str = self.sess.run(tf.summary.merge_all())
            self.writer.add_summary(summary_str, ep)

    # initialize session
    opt = Option(0)

    #####################################################################################################
    ## Training

    total_steps = 0

    epsilon = epsilon_start
    epsilon_linear_step = (epsilon_start-epsilon_end)/epsilon_decay_length

    start_time = time.time()
    for ep in range(num_episodes):

        total_reward = 0
        steps_in_ep = 0

        observation = env.reset()

        for t in range(max_steps_ep):

            # choose action according to epsilon-greedy policy wrt Q
            if np.random.random() < epsilon:
                action = np.random.randint(n_actions)
            else:
                q_s = opt.sess.run(q_action_values, feed_dict = {state_ph: observation[None], is_training_ph: False})
                action = np.argmax(q_s)

            # take step
            next_observation, reward, done, _info = env.step(action)
            if args.visualize:
                env.render()
            total_reward += reward

            # add this to experience replay buffer
            opt.experience.append((observation, action, reward, next_observation, 0.0 if done else 1.0))
            # update the slow target's weights to match the latest q network if it's time to do so
            if total_steps%update_slow_target_every == 0:
                _ = opt.sess.run(update_slow_target_op)

            # update network weights to fit a minibatch of experience
            if total_steps%train_every == 0 and len(opt.experience) >= minibatch_size:

                # grab N (s,a,r,s') tuples from experience
                minibatch = random.sample(opt.experience, minibatch_size)

                # do a train_op with all the inputs required
                
                _ = opt.sess.run(train_op,
                    feed_dict = {
                        state_ph: np.asarray([elem[0] for elem in minibatch]),
                        action_ph: np.asarray([elem[1] for elem in minibatch]),
                        reward_ph: np.asarray([elem[2] for elem in minibatch]),
                        next_state_ph: np.asarray([elem[3] for elem in minibatch]),
                        is_not_terminal_ph: np.asarray([elem[4] for elem in minibatch]),
                        is_training_ph: True})
            observation = next_observation
            total_steps += 1
            steps_in_ep += 1

            # linearly decay epsilon from epsilon_start to epsilon_end over epsilon_decay_length steps
            if total_steps < epsilon_decay_length:
                epsilon -= epsilon_linear_step
            # then exponentially decay it every episode
            elif done:
                epsilon *= epsilon_decay_exp

            if total_steps == epsilon_decay_length:
                print('--------------------------------MOVING TO EXPONENTIAL EPSILON DECAY-----------------------------------------')

            if done:
                # Increment episode counter
                _ = opt.sess.run(episode_inc_op)
                break

        opt.writeReward(total_reward, ep)

        print('Episode %2i, Reward: %7.3f, Steps: %i, Next eps: %7.3f, Minutes: %7.3f'%\
            (ep,total_reward,steps_in_ep, epsilon, (time.time() - start_time)/60))

    env.close()

if __name__ == '__main__':
    main()