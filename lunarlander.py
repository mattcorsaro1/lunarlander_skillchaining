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
    parser.set_defaults(visualize=False)
    parser.add_argument("--model", type=str, default="")
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

    # game parameters
    env = gym.make("LunarLander-v2")
    state_dim = np.prod(np.array(env.observation_space.shape))
    n_actions = env.action_space.n

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

    plot_epsilon = tf.Variable(0.)
    tf.summary.scalar("Epsilon", plot_epsilon)
    eps_summary_placeholder = tf.placeholder("float")
    update_plot_epsilon = plot_epsilon.assign(eps_summary_placeholder)

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

    # initialize session
    sess = tf.Session()
    sess.run(tf.global_variables_initializer())

    

    timestamp = datetime.datetime.fromtimestamp(time.time()).strftime('%Y_%m_%d_%H_%M_%S')
    board_name = "board_" + timestamp
    saver = tf.train.Saver()
    start_time = time.time()
    if args.model == '':
        #####################################################################################################
        ## Training
        print "Training a new model."
        writer = tf.summary.FileWriter(board_name)
        writer.add_graph(sess.graph)

        total_steps = 0
        experience = deque(maxlen=replay_memory_capacity)

        epsilon = epsilon_start
        epsilon_linear_step = (epsilon_start-epsilon_end)/epsilon_decay_length
        for ep in range(num_episodes):

            total_reward = 0
            steps_in_ep = 0

            observation = env.reset()

            for t in range(max_steps_ep):

                # choose action according to epsilon-greedy policy wrt Q
                if np.random.random() < epsilon:
                    action = np.random.randint(n_actions)
                else:
                    q_s = sess.run(q_action_values,
                        feed_dict = {state_ph: observation[None], is_training_ph: False})
                    action = np.argmax(q_s)

                # take step
                next_observation, reward, done, _info = env.step(action)
                if args.visualize:
                    env.render()
                total_reward += reward

                # add this to experience replay buffer
                experience.append((observation, action, reward, next_observation, 0.0 if done else 1.0))

                # update the slow target's weights to match the latest q network if it's time to do so
                if total_steps%update_slow_target_every == 0:
                    _ = sess.run(update_slow_target_op)

                # update network weights to fit a minibatch of experience
                if total_steps%train_every == 0 and len(experience) >= minibatch_size:

                    # grab N (s,a,r,s') tuples from experience
                    minibatch = random.sample(experience, minibatch_size)

                    # do a train_op with all the inputs required
                    _ = sess.run(train_op,
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
                    _ = sess.run(episode_inc_op)
                    break

            sess.run(update_ep_reward, feed_dict={r_summary_placeholder: total_reward})
            sess.run(update_plot_epsilon, feed_dict={eps_summary_placeholder: epsilon})
            summary_str = sess.run(tf.summary.merge_all())
            writer.add_summary(summary_str, ep)

            print('Episode %2i, Reward: %7.3f, Steps: %i, Next eps: %7.3f, Minutes: %7.3f'%\
                (ep,total_reward,steps_in_ep, epsilon, (time.time() - start_time)/60))

        saver.save(sess, os.getcwd() + '/' + timestamp + ".ckpt")
    else:
        print "Loading trained model from", args.model
        saver.restore(sess, os.getcwd() + '/' + args.model)
        attempts = 10
        print "Load successful, playing for", attempts, "games."
        for _ in range(attempts):
            observation = env.reset()
            total_reward = 0
            steps = 0
            for t in range(max_steps_ep):
                q_s = sess.run(q_action_values, feed_dict = {state_ph: observation[None], is_training_ph: False})
                action = np.argmax(q_s)
                next_observation, reward, done, _info = env.step(action)
                observation = next_observation
                total_reward += reward
                env.render()
                steps += 1
                if done:
                    break
            print "Reward:", total_reward, "in", steps, "steps."
    env.close()

if __name__ == '__main__':
    main()
