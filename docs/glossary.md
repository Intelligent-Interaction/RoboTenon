# RoboTenon glossary

[**English**](glossary.md) · [简体中文](glossary.zh-CN.md)

Use this page to read the documentation. The corresponding reference guides give the exact API definitions and field types.

## Names and interfaces

| Name | Meaning |
| --- | --- |
| RoboTenon | The project and framework |
| `robotenon` | The Python package. Use `Tenon` to run its command-line interface. |
| CLI | The command-line interface, for example `Tenon run-fake ...` |
| Python API | Functions and classes you import into your own Python program |
| Web Portal | Browser interface for submitting, inspecting, and controlling experiments |

## Objects in a run

| Name | Meaning | Example |
| --- | --- | --- |
| Task | A goal to achieve | Put the cup in the cabinet |
| Episode | One trial of a task | Try the task once with seed 7 |
| Experiment | A collection of episodes | Run 10 tasks three times each |
| Run | One launch, usually with a distinct `run_id` and output directory | `cpu-demo-02` |
| Attempt | One execution attempt for an episode. Retrying creates another attempt | The first process crashed, and the retry succeeded |
| Seed | A value that helps reproduce the same initial random conditions | `7`, `8`, `9` |

## Runtime components

| Name | Meaning |
| --- | --- |
| Config | The manifest that selects components, settings, and workflow |
| TaskAdapter | Enumerates the tasks and how often to run each one in an experiment |
| Worker | An execution process that holds runtime resources such as environments or models |
| Environment | The robot's world, which can be Fake, simulated, or physical |
| Session | One connection and interaction lifecycle with an Environment |
| Body | Sends actions such as a movement command to the simulator or robot |
| Graph | A workflow describing order, branches, loops, and termination conditions |
| Node | A step in a Graph |
| Module | A reusable application step for perception, planning, decision-making, or evaluation |
| Tool | A capability a Module calls, such as a model, memory store, or skill library |
| Monitor | Observes the running system and can raise an alert or trigger a safety stop |
| Evaluator | Assesses the result, for example whether the object reached its target area |
| Extension | A separately installed package for a simulator, model, device, or task set |
| Provider | An implementation an extension registers with the framework, such as an Environment or Tool |

## Results and diagnostics

| Name | Meaning |
| --- | --- |
| Output / Result | Structured data returned at the end of the task |
| Trace | A timeline of when each step started and finished and whether it succeeded or failed |
| Metric | A measurement such as success rate, duration, or throughput |
| Observation | Information from the environment, such as images, joint state, or task state |
| Feedback | The environment's report about an action or state, such as action completion or environment health |
| Checkpoint | Intermediate state saved to resume execution |
| Artifact | A file a run produces, such as a result JSON, video, log, or configuration snapshot |

## Workflow completion and task success

Read the completion and success fields independently.

- `workflow_status: completed` or `graph_status: completed` means the workflow reached its end.
- `success: true` or `task_success: true` means the task goal was achieved.

For example, a workflow can finish every step while the robot fails to grasp the cup. Its status is `completed` and the task outcome is `false`. Check the workflow status first, then the task result and `reason`.

## Environment types

- **Fake** exercises workflow behavior without physics or device control.
- **Simulation** executes in a simulated world such as RoboCasa, LIBERO, or RoboDojo.
- **Physical robot** reads sensors and sends actions through device drivers.

All three use the same configuration, Graph, entry points, and result format. Typically, teams verify the workflow with Fake components, validate the task in simulation, and then test the physical robot.
