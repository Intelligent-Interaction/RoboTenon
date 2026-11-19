"""Generic official RoboDojo evaluation loop for a RoboTenon policy server."""


def eval_one_episode(TASK_ENV, model_client):
    model_client.call(func_name="reset")
    while not TASK_ENV.is_episode_end():
        model_client.call(func_name="update_obs", obs=TASK_ENV.get_obs())
        actions = model_client.call(func_name="get_action")
        for index, action in enumerate(actions):
            TASK_ENV.take_action(action)
            if TASK_ENV.is_episode_end() or index + 1 == len(actions):
                break
            model_client.call(func_name="update_obs", obs=TASK_ENV.get_obs())


def eval_one_episode_batch(TASK_ENV, model_client):
    model_client.call(func_name="reset")
    while not TASK_ENV.is_episode_end():
        indices = TASK_ENV.get_running_env_idx_list()
        model_client.call(func_name="update_obs_batch", obs=TASK_ENV.get_obs_batch(indices))
        chunks = model_client.call(func_name="get_action_batch", obs=indices)
        for offset in range(len(chunks[0])):
            TASK_ENV.take_action_batch([chunk[offset] for chunk in chunks], indices)
            if TASK_ENV.is_episode_end() or offset + 1 == len(chunks[0]):
                break
