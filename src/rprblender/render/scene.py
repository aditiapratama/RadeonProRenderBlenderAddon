import itertools
import operator
import sys
import threading
import time

import numpy as np
import pyrpr

import rprblender.render
import rprblender.render
import rprblender.render.render_layers
import rprblender.render.device
from rprblender import helpers, config
from rprblender import logging
from rprblender.helpers import CallLogger

call_logger = CallLogger(tag='render.scene')


class SceneRenderer:

    @property
    def core_context(self):
        return self.render_device.core_context

    @property
    def post_effects(self):
        return self.render_device.post_effects

    render_targets = None
    render_layers = None

    def __init__(self, render_device, rs, is_production=False):
        self.render_device = render_device

        self.posteffect_chain = rprblender.render.device.PostEffectChain(self.render_device)

        self.im = None
        self.im_tile = None
        self.im_iteration = None
        self.im_prepared = {}
        self.iteration_in_progress = None
        self.cache_generated = False
        self.time_in_progress = None
        self.resolution = None  # type: tuple
        self.region = None
        self.time_render_start = None

        self.render_settings = rs
        self.production_render = False
        self.aov_settings = None
        self.tile_image = None

        self.is_production = is_production
        self.used_iterations = 1
        self.iteration_divider = 1

    @call_logger.logged
    def __del__(self):
        self.render_layers = None
        if self.render_targets is not None:
            self.render_device.detach_render_target(self.render_targets)
            del self.render_targets

        del self.posteffect_chain
        del self.render_device

    def get_core_context(self):
        return self.core_context

    @call_logger.logged
    def update_render_resolution(self, render_resolution):
        self.render_layers = None
        if self.render_targets is not None:
            self.render_device.detach_render_target(self.render_targets)
            del self.render_targets

        self.resolution = render_resolution
        self.render_targets = rprblender.render.device.RenderTargets(self.render_device, self.resolution)
        self.render_layers = rprblender.render.render_layers.RenderLayers(
            self.aov_settings, self.render_targets)

        self.render_device.attach_render_target(self.render_targets)

    @call_logger.logged
    def update_render_region(self, render_region):
        self.region = render_region

    @call_logger.logged
    def update_aov(self, aov):
        self.aov_settings = aov

    def update_tone_mapping(self, settings, post_effect_update):

        tm = settings.tone_mapping
        if not tm.enable:
            return False

        if tm.type == 'simplified':
            simple_tonemap = post_effect_update.enable(pyrpr.POST_EFFECT_SIMPLE_TONEMAP)
            simple_tonemap.set_param_float(b"exposure", tm.simplified.exposure)
            simple_tonemap.set_param_float(b"contrast", tm.simplified.contrast)

            pyrpr.ContextSetParameter1u(self.get_core_context(), b"tonemapping.type",
                                        pyrpr.TONEMAPPING_OPERATOR_NONE)

            return True

        elif tm.type == 'linear':

            post_effect_update.enable(pyrpr.POST_EFFECT_TONE_MAP)

            pyrpr.ContextSetParameter1u(self.get_core_context(), b"tonemapping.type",
                                        pyrpr.TONEMAPPING_OPERATOR_PHOTOLINEAR)

            pyrpr.ContextSetParameter1f(self.get_core_context(), b'tonemapping.photolinear.sensitivity',
                                        tm.linear.iso * 0.01)
            pyrpr.ContextSetParameter1f(self.get_core_context(), b'tonemapping.photolinear.exposure',
                                        tm.linear.shutter_speed)
            pyrpr.ContextSetParameter1f(self.get_core_context(), b'tonemapping.photolinear.fstop', tm.linear.f_stop)

            logging.info('tm.linear.iso: %d, tm.linear.shutter_speed: %f, tm.linear.f_stop %f' %
                         (tm.linear.iso, tm.linear.shutter_speed, tm.linear.f_stop))

            return True

        elif tm.type == 'non_linear':

            post_effect_update.enable(pyrpr.POST_EFFECT_TONE_MAP)

            pyrpr.ContextSetParameter1u(self.get_core_context(), b"tonemapping.type",
                                        pyrpr.TONEMAPPING_OPERATOR_REINHARD02)

            pyrpr.ContextSetParameter1f(self.get_core_context(), b'tonemapping.reinhard02.prescale',
                                        tm.nonlinear.prescale)
            pyrpr.ContextSetParameter1f(self.get_core_context(), b'tonemapping.reinhard02.postscale',
                                        tm.nonlinear.postscale)
            pyrpr.ContextSetParameter1f(self.get_core_context(), b'tonemapping.reinhard02.burn',
                                        tm.nonlinear.burn)

            logging.info('tm.nonlinear.prescale: %f, tm.nonlinear.postscale: %f, tm.nonlinear.burn %f' %
                         (tm.nonlinear.prescale, tm.nonlinear.postscale, tm.nonlinear.burn))

            return True

        else:
            assert False, 'unknown tonemapping type'

        return False

    def update_white_balance(self, settings, post_effect_update):

        wb = settings.white_balance
        if not wb.enable:
            return False

        white_balance = post_effect_update.enable(pyrpr.POST_EFFECT_WHITE_BALANCE)
        white_balance.set_param_int(b"colorspace", wb.color_space_values[wb.color_space])
        white_balance.set_param_float(b"colortemp", wb.color_temperature)

        return True

    def update_gamma_correction(self, settings, post_effect_update):

        gm = settings.gamma_correction
        if not gm.enable:
            return False

        if gm.viewport_only and self.production_render:
            return False

        post_effect_update.enable(pyrpr.POST_EFFECT_GAMMA_CORRECTION)
        pyrpr.ContextSetParameter1f(self.get_core_context(), b'displaygamma', gm.display_gamma)

        return True

    def render_proc(self):
        yield from self._render_proc()

    def _render_proc(self):

        from rprblender import properties

        #raise Exception("hello from render_proc")

        rs = self.render_settings

        #AA-Sample and Iteration limit recalculated based on: AMDBLENDER-659
        ##iterations = (#user set iterations) * (#user set samples) / #samples
        settings = helpers.get_user_settings()
        numGPUs = helpers.get_used_gpu_count(settings.gpu_states)
        user_set_samples = settings.samples
        if rs.rendering_limits.enable:
            if 'ITER' == rs.rendering_limits.type:
                if self.is_production and settings.device_type == 'gpu' and settings.device_type_plus_cpu:
                    samples = 100
                else:
                    # if production(final) render force sample count to GPU count for better throughput
                    # don't force it in viewport render for better interactivity(mGPU sync takes time)
                    if numGPUs > user_set_samples and self.is_production:
                        samples = numGPUs
                    else:
                        samples = user_set_samples

                self.used_iterations = int(rs.rendering_limits.iterations * user_set_samples / samples)
                self.iteration_divider = user_set_samples / samples
                if self.used_iterations < 1:
                    self.used_iterations = 1
                    self.iteration_divider = 1

            else:
                samples = user_set_samples
        else:
            samples = user_set_samples

        time_start = time.perf_counter()
        self.time_render_start = time_start
        time_local_total = 0

        self.iteration_in_progress = None

        render_resolution = self.resolution

        if self.region is not None:
            render_region = np.uint32(np.concatenate(np.multiply(self.region, [[render_resolution[0]], [render_resolution[1]]])))
        else:
            render_region = None

        self.log_debug("render_proc: redraw", self.resolution)

        timestamp_operation_last = time.perf_counter()

        with rprblender.render.core_operations(raise_error=True):

            pyrpr.ContextSetParameter1u(self.get_core_context(), b"rendermode",
                                          properties.RenderSettings.rendermode_remap[rs.render_mode])

            pyrpr.ContextSetParameter1u(self.get_core_context(), b"aasamples", samples)

            if rs.global_illumination.use_clamp_irradiance:
                pyrpr.ContextSetParameter1f(self.get_core_context(), b"radianceclamp",
                                              rs.global_illumination.clamp_irradiance);
            else:
                pyrpr.ContextSetParameter1f(self.get_core_context(), b"radianceclamp",
                                              sys.float_info.max);

            pyrpr.ContextSetParameter1u(self.get_core_context(), b"maxRecursion", rs.get_max_ray_depth(self.production_render))

            pyrpr.ContextSetParameter1u(self.get_core_context(),
                                          b"imagefilter.type",
                                          properties.AntiAliasingSettings.filter_remap[rs.aa.filter])

            if rs.aa.filter in properties.AntiAliasingSettings.radius_params:
                pyrpr.ContextSetParameter1f(self.get_core_context(),
                                              properties.AntiAliasingSettings.radius_params[rs.aa.filter],
                                              rs.aa.radius)

        timstamp_operation = time.perf_counter()
        time_local_total += timstamp_operation - timestamp_operation_last
        timestamp_operation_last = timstamp_operation

        with rprblender.render.core_operations(raise_error=True):
            self.render_targets.clear()

        for i in itertools.count():
            rendering_limits = rs.rendering_limits
            if rendering_limits.enable:
                if 'TIME' == rendering_limits.type:
                    if rendering_limits.time != 0 and rendering_limits.time <= (time.perf_counter() - time_start):
                        break
                elif 'ITER' == rendering_limits.type:
                    if self.used_iterations != 0 and self.used_iterations <= i:
                        break

            self.iteration_in_progress = i
            self.time_in_progress = time.perf_counter() - time_start
            self.log_debug('render_proc inner loop iteration')
            timestamp_operation_last = time.perf_counter()

            with rprblender.render.core_operations(raise_error=True):
                if render_region is None:
                    pyrpr.ContextRender(self.get_core_context())
                else:
                    pyrpr.ContextRenderTile(self.get_core_context(), *render_region)
            self.cache_generated = True

            self.im_tile = self.tile_image
            self.im_iteration = i
            self.im_prepared.clear()

            timstamp_operation = time.perf_counter()
            time_local_total += timstamp_operation - timestamp_operation_last
            timestamp_operation_last = timstamp_operation
            self.log_debug('render_proc inner loop iteration done:', self.iteration_in_progress)

            yield False

            self.log_debug('render_proc inner loop iteration wait')
        self.log_debug('render_proc loops completed')

        self.log_debug('render_proc calc time:')
        time_delta = time.perf_counter() - time_start
        self.log_debug('render_proc calc time ok', time_delta, time_local_total)
        self.log_debug('render_proc completed in %.2fs, used %.2fs (%3.1f%% cpu)'
                       % (time_delta, time_local_total, 100 * time_local_total / time_delta))
        self.log_debug('render_proc log time ok')


    def log_debug(self, *args):
        logging.debug(*args, tag='render.proc')

    def get_image_tile(self):
        return self.im_tile

    def get_image(self, aov_name='default'):

        if aov_name in self.im_prepared:
            return self.im_prepared[aov_name]

        with rprblender.render.core_operations(raise_error=True):

            frame_buffer = self.render_targets.get_frame_buffer(aov_name)

            if not frame_buffer:
                return

            # apply post effects, remaking posteffects chain for each pass separately
            # RPR will have per-buffer posteffect chains later, but now they are on the context
            # so need to be reattached separately for every aov
            post_effect_chain = self.posteffect_chain
            post_effect_update = post_effect_chain.start_update()
            # Always apply normalization, aov need this too.
            post_effect_update.enable(pyrpr.POST_EFFECT_NORMALIZATION)

            if aov_name == 'default':
                settings = self.render_settings
                self.update_tone_mapping(settings, post_effect_update)
                self.update_white_balance(settings, post_effect_update)
                self.update_gamma_correction(settings, post_effect_update)

            im = self.render_targets.get_resolved_image(frame_buffer)

            # dummy_render = False
            #
            # if dummy_render:  # render simple animated gradient
            #     im = np.ones((height, width, 4), dtype=np.float32)
            #     im[:, :, 2] = np.sin(10 * np.pi * (t + np.linspace(0, 1, height, dtype=np.float32)))[:, np.newaxis]
            #     im[:, :, 0] = np.linspace(0, 1, height, dtype=np.float32)[:, np.newaxis]
            #     im[:, :, 1] = np.linspace(0, 1, width, dtype=np.float32)[np.newaxis, :]
            #     im[:, :, 3] = 1

        if im is not None:
            self.im_prepared[aov_name] = self.render_layers.prepare_image_by_layer(aov_name, im)

        return self.im_prepared.get(aov_name)

class RenderThread(threading.Thread):

    def __init__(self):
        super().__init__()

        self.terminate_event = threading.Event()
        self.terminate_event.clear()

    def terminate(self):
        self.terminate_event.set()

    def run(self):
        logging.debug(self, 'run', tag='render.scene')
        while not self.terminate_event.wait(timeout=0.0001):
            self.renderer.render_proc()
        logging.debug(self, 'run complete', tag='render.scene')

class UpdateBlock:

    def __init__(self, value=None, has_value=True, equal=operator.eq):
        self.value = value
        self.has_value = has_value
        self.equal = equal

    def __str__(self):
        return "UpdateBlock(value=%s)" % (self.value,)

    @call_logger.logged
    def set_value(self, value):
        self.has_value = True
        self.value = value

    @call_logger.logged
    def pop_value(self):
        self.has_value = False
        return self.value

    def del_value(self):
        self.has_value = False


class UpdateData:

    def __init__(self):
        self.render_region = UpdateBlock(value=None, equal=np.array_equal)
        self.render_resolution = UpdateBlock(has_value=False, equal=np.array_equal)
        self.aov = UpdateBlock(has_value=False,
                               equal=lambda old, new: old is not None and old == new)
        self.render_camera = UpdateBlock(has_value=False, equal=lambda old, new: old is not None and old.is_same(new))

    def update_block(self, block, block_value_current, block_value_new):
        """ """
        equal = block.equal

        if block.has_value:
            # if update queued already has same data - skip it
            if equal(block.value, block_value_new):
                return

        # if value is already set - skip and clear update data
        if equal(block_value_current, block_value_new):
            # optimization - skip update value that was queued(but not applied yet) and is overriden by new value
            if block.has_value:
                block.del_value()
            return

        block.set_value(block_value_new)


class SceneRendererThreaded:

    def __init__(self, scene_renderer):
        self.scene_renderer = scene_renderer  # type: SceneRenderer

        self._need_scene_redraw = False

        self.thread = None  # type: RenderThread
        self.update_lock = threading.Lock()
        self.image_lock = threading.Lock()
        self.render_completed_event = threading.Event()

        self.update_data_lock = threading.Lock()
        self.update_data = UpdateData()
        self.render_resolution = None
        self.aov = None
        self.render_region = None
        self.render_camera = None

        self.scene_synced = None

    @call_logger.logged
    def __del__(self):
        self.stop()

    def _set_need_scene_redraw(self, value):

        self.render_completed_event.clear()
        self._need_scene_redraw = value

    need_scene_redraw = property(fset=_set_need_scene_redraw)

    def log_debug(self, *args):
        logging.debug(*args, tag='render.proc')

    def is_render_completed(self):
        # in case render crashed
        if not self.is_alive():
            return True
        return self.render_completed_event.is_set()

    def is_alive(self):
        # in case render crashed
        return self.thread.is_alive()

    sleep_delay_interactive = 0.01
    sleep_delay_noninteractive = 0.0

    def start(self):
        self.sleep_delay = self.sleep_delay_interactive
        self._start()

    def start_noninteractive(self):
        self.sleep_delay = self.sleep_delay_noninteractive
        self.need_scene_redraw = True
        self._start()

    @call_logger.logged
    def _start(self):
        self.stop_requested = False
        self.thread = RenderThread()
        self.thread.renderer = self
        self.thread.start()

    @call_logger.logged
    def stop(self):
        self.stop_requested = True
        if self.thread:
            self.thread.terminate()
            self.thread.join()
            self.thread = None #break reference cycle
            self.log_debug(self, 'thread stopped')

    def render_proc(self):
        render_iter = self.scene_renderer.render_proc()

        with self.update_lock:
            self.check_updates()

            if not self._need_scene_redraw:
                return
            self._need_scene_redraw = False
            self.render_completed_event.clear()

        while not self.stop_requested:
            try:
                with self.update_lock:
                    self.check_updates()

                    if self._need_scene_redraw:
                        self.log_debug('render_proc inner loop break - need_scene_redraw')
                        return
                    next(render_iter)

                    #s = cProfile.runctx("next(render_iter)", globals(), locals(), sort='cumulative')

                time.sleep(self.sleep_delay)
            except StopIteration:
                break

        logging.debug('render completed', tag='render.proc')
        self.render_completed_event.set()

    def render_proc_noninteractive(self):
        self.render_completed_event.clear()

        for _ in self.scene_renderer.render_proc():
            if self.stop_requested:
                break
            time.sleep(0)
        self.render_completed_event.set()

        logging.debug('render completed', tag='render.proc')

    def check_updates(self):
        with self.update_data_lock:
            if self.update_data.render_resolution.has_value:
                logging.debug('resolution changed to ', self.update_data.render_resolution,  tag='render.proc.update')
                self.render_resolution = self.update_data.render_resolution.pop_value()

                # this partially duplicates code below for aov, only not if resolution changed there's no
                # need to partially update aovs - all will be recreated
                if self.update_data.aov.has_value:
                    logging.debug('aov changed to ', self.update_data.aov,  tag='render.proc.update')
                    self.aov = self.update_data.aov.pop_value()
                    self.scene_renderer.aov_settings = self.aov

                with self.image_lock:
                    self.scene_renderer.update_render_resolution(self.render_resolution)
                self.need_scene_redraw = True

            elif self.update_data.aov.has_value:
                logging.debug('aov changed to ', self.update_data.aov,  tag='render.proc.update')
                self.aov = self.update_data.aov.pop_value()
                self.scene_renderer.aov_settings = self.aov

                self.scene_renderer.render_layers.update(self.aov)
                self.need_scene_redraw = True


            if self.update_data.render_region.has_value:
                logging.debug('render_region changed to ', self.update_data.render_region,  tag='render.proc.update')
                self.render_region = self.update_data.render_region.pop_value()
                self.scene_renderer.update_render_region(self.render_region)
                self.need_scene_redraw = True

            if self.update_data.render_camera.has_value:
                logging.debug('render_camera changed to ', self.update_data.render_camera,  tag='render.proc.update')
                self._set_render_camera(self.update_data.render_camera.pop_value())
                self.need_scene_redraw = True

    @call_logger.logged
    def set_render_resolution(self, render_resolution):
        self.render_resolution = render_resolution
        self.scene_renderer.update_render_resolution(self.render_resolution)

    @call_logger.logged
    def update_render_resolution(self, render_resolution):
        self.update_block(self.update_data.render_resolution, self.render_resolution, render_resolution)

    @call_logger.logged
    def set_render_region(self, render_region):
        self.render_region = render_region
        self.scene_renderer.update_render_region(self.render_region)

    @call_logger.logged
    def update_render_region(self, render_region):
        self.update_block(self.update_data.render_region, self.render_region, render_region)

    @call_logger.logged
    def set_aov(self, aov):
        self.scene_renderer.update_aov(aov)

    @call_logger.logged
    def update_aov(self, aov):
        self.update_block(self.update_data.aov, self.aov, aov)

    def _set_render_camera(self, camera):
        self.render_camera = camera
        self.scene_synced.set_render_camera(camera)
        if self.scene_synced.camera_zoom is not None:
            self.scene_renderer.tile_image = (
                self.scene_synced.camera_zoom,
                self.scene_synced.camera_zoom)
        else:
            self.scene_renderer.tile_image = (1, 1)

    def update_render_camera(self, render_camera):
        self.update_block(self.update_data.render_camera, self.render_camera, render_camera)

    def update_block(self, block, block_value_current, block_value_new):
        with self.update_data_lock:
            self.update_data.update_block(block, block_value_current, block_value_new)

    def set_scene_synced(self, scene_synced):
        self.scene_synced = scene_synced
