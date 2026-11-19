"""Keep robosuite renderer finalization in its owning EGL context.

robosuite 1.5.2 frees MjrContext without first binding its GL context. A
cyclic renderer from an earlier reset can therefore delete resources belonging
to the live renderer when Python collects it during a rollout.
"""

from functools import wraps
import os


def _guard_finalizer(finalizer, egl):
    @wraps(finalizer)
    def cleanup(renderer):
        gl = getattr(renderer, "gl_ctx", None)
        owner = getattr(gl, "_context", None)
        if not owner:
            return finalizer(renderer)
        previous = egl.eglGetCurrentContext()
        display = egl.eglGetCurrentDisplay()
        draw = egl.eglGetCurrentSurface(egl.EGL_DRAW)
        read = egl.eglGetCurrentSurface(egl.EGL_READ)
        restore = bool(previous) and previous.address != owner.address
        try:
            gl.make_current()
            return finalizer(renderer)
        finally:
            # EGLGLContext.free() releases the thread. Collection may occur
            # inside another render, so restore that live context and surfaces.
            if restore:
                egl.eglMakeCurrent(display, draw, read, previous)

    cleanup._robotenon_egl_cleanup = True
    return cleanup


def install_egl_cleanup_guard():
    if os.environ.get("MUJOCO_GL", "").lower() != "egl":
        return
    from OpenGL import EGL
    from robosuite.utils.binding_utils import MjRenderContext

    original = MjRenderContext.__del__
    if not getattr(original, "_robotenon_egl_cleanup", False):
        MjRenderContext.__del__ = _guard_finalizer(original, EGL)
