# Physical robot integration

[**English**](real-robot.md) · [简体中文](real-robot.zh-CN.md)

The core package provides `RealRobotAdapter`, `RealRobotBody`, safety state, and an observation cache.
The older XR365 planner/verifier master configuration for hardware has been retired. Provide a site-specific
extension containing the driver, camera source, action format, and safety policy.

Register the following components in the site extension:

- **Environment** reads `EnvironmentFeedback` from the robot and cameras.
- **Body** sends `ActionChunk` to the vendor driver and implements hold and stop.
- **TaskAdapter** enumerates site tasks and trials.
- **VLA or Tool** produces actions compatible with `EnvironmentIO.action`.
- **Graph profile** defines reset, inference, execution, observation, and termination.

Use a dry-run driver or hardware simulation to verify I/O schemas, action dimensions, timeouts, emergency stops, and disconnect recovery.
Then validate the physical device with the site extension. Generated benchmark configurations target simulation
evaluation and require adaptation before hardware deployment.
