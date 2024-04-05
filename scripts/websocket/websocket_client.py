import zlib
import torch
import asyncio
import threading
import websockets
import numpy as np
from copy import deepcopy
from torchvision.io import encode_jpeg, decode_jpeg

from glm import vec3, vec4, mat3, mat4, mat4x3
from imgui_bundle import imgui_color_text_edit as ed
from imgui_bundle import portable_file_dialogs as pfd
from imgui_bundle import imgui, imguizmo, imgui_toggle, immvision, implot, ImVec2, ImVec4, imgui_md, immapp, hello_imgui

from easyvolcap.utils.console_utils import *
from easyvolcap.utils.viewer_utils import Camera, CameraPath
from easyvolcap.utils.data_utils import add_iter, add_batch, to_cuda, Visualization

# fmt: off
from easyvolcap.runners.volumetric_video_viewer import VolumetricVideoViewer
import glfw
# fmt: on


class Viewer(VolumetricVideoViewer):
    def __init__(self,
                 window_size: List[int] = [1080, 1920],  # height, width
                 window_title: str = f'Neural Cosmos',  # MARK: global config
                 exp_name: str = 'random',

                 font_size: int = 18,
                 font_bold: str = 'assets/fonts/CascadiaCodePL-Bold.otf',
                 font_italic: str = 'assets/fonts/CascadiaCodePL-Italic.otf',
                 font_default: str = 'assets/fonts/CascadiaCodePL-Regular.otf',
                 icon_file: str = 'assets/imgs/easyvolcap.png',

                 use_window_focal: bool = True,
                 use_quad_cuda: bool = True,
                 use_quad_draw: bool = False,

                 update_fps_time: float = 0.5,  # be less stressful
                 update_mem_time: float = 0.5,  # be less stressful

                 skip_exception: bool = False,  # always pause to give user a debugger
                 compose: bool = False,
                 compose_power: float = 1.0,
                 render_meshes: bool = True,
                 render_network: bool = True,

                 mesh_preloading: List[str] = [],
                 splat_preloading: List[str] = [],
                 show_preloading: bool = True,

                 fullscreen: bool = False,
                 camera_cfg: dotdict = dotdict(type=Camera.__name__),

                 show_metrics_window: bool = False,
                 show_demo_window: bool = False,

                 visualize_axes: bool = False,  # will add an extra 0.xms
                 ):
        # Camera related configurations
        self.camera_cfg = camera_cfg
        self.fullscreen = fullscreen
        self.window_size = window_size
        self.window_title = window_title
        self.use_window_focal = use_window_focal

        # Quad related configurations
        self.use_quad_draw = use_quad_draw
        self.use_quad_cuda = use_quad_cuda
        self.compose = compose  # composing only works with cudagl for now
        self.compose_power = compose_power

        # Font related config
        self.font_default = font_default
        self.font_italic = font_italic
        self.font_bold = font_bold
        self.font_size = font_size
        self.icon_file = icon_file

        self.render_meshes = render_meshes
        self.render_network = render_network

        self.update_fps_time = update_fps_time
        self.update_mem_time = update_mem_time

        self.exposure = 1.0
        self.offset = 0.0
        self.use_vsync = False

        self.init_camera(camera_cfg)  # prepare for the actual rendering now, needs dataset -> needs runner
        self.init_glfw()  # ?: this will open up the window and let the user wait, should we move this up?
        self.init_imgui()

        from easyvolcap.engine import args
        args.type = 'gui'  # manually setting this parameter
        self.init_opengl()
        self.init_quad()
        self.bind_callbacks()

        from easyvolcap.utils.gl_utils import Mesh, Splat

        self.meshes: List[Mesh] = [
            *[Mesh(filename=mesh, visible=show_preloading, render_normal=True) for mesh in mesh_preloading],
            *[Splat(filename=splat, visible=show_preloading, point_radius=0.0015, H=self.H, W=self.W) for splat in splat_preloading],
        ]

        self.camera_path = CameraPath()
        self.visualize_axes = visualize_axes
        self.visualize_paths = True
        self.visualize_cameras = True
        self.visualize_bounds = True
        self.epoch = 0
        self.runner = dotdict(ep_iter=0, collect_timing=False, timer_record_to_file=False, timer_sync_cuda=True)
        self.dataset = dotdict()
        self.visualization_type = Visualization.RENDER
        self.playing = False
        self.discrete_t = False
        self.playing_speed = 0.0
        self.network_available = False

        # Initialize other parameters
        self.show_demo_window = show_demo_window
        self.show_metrics_window = show_metrics_window

        # Others
        self.skip_exception = skip_exception
        self.static = dotdict(batch=dotdict(), output=dotdict())  # static data store updated through the rendering
        self.dynamic = dotdict()

    def init_camera(self, camera_cfg: dotdict):
        self.camera = Camera(**camera_cfg)
        self.camera.front = self.camera.front  # perform alignment correction

    def frame(self):
        # print(f'framing: {time.perf_counter()}')
        import OpenGL.GL as gl
        gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT)
        self.dynamic = dotdict()

        # Render GS
        if self.render_network:
            buffer = image
            if buffer is not None:
                buffer = buffer.permute(1, 2, 0)
                buffer = torch.cat([buffer, torch.ones_like(buffer[..., :1])], dim=-1)
                self.quad.copy_to_texture(buffer)
                self.quad.draw()

        # Render meshes (or point clouds)
        if self.render_meshes:
            for mesh in self.meshes:
                mesh.render(self.camera)

        self.draw_imgui()  # defines GUI elements
        self.show_imgui()

    def draw_rendering_gui(self, batch: dotdict = dotdict(), output: dotdict = dotdict()):

        # Other rendering options like visualization type
        if imgui.collapsing_header('Rendering'):
            self.visualize_axes = imgui_toggle.toggle('Visualize axes', self.visualize_axes, config=self.static.toggle_ios_style)[1]
            self.visualize_bounds = imgui_toggle.toggle('Visualize bounds', self.visualize_bounds, config=self.static.toggle_ios_style)[1]
            self.visualize_cameras = imgui_toggle.toggle('Visualize cameras', self.visualize_cameras, config=self.static.toggle_ios_style)[1]

    def draw_imgui(self):
        from easyvolcap.utils.gl_utils import Mesh, Splat, Gaussian

        # Initialization
        glfw.poll_events()  # process pending events, keyboard and stuff
        imgui.backends.opengl3_new_frame()
        imgui.backends.glfw_new_frame()
        imgui.new_frame()
        imgui.push_font(self.default_font)

        self.static.playing_time = self.camera_path.playing_time  # Remember this, if changed, update camera
        self.static.slider_width = imgui.get_window_width() * 0.65  # https://github.com/ocornut/imgui/issues/267
        self.static.toggle_ios_style = imgui_toggle.ios_style(size_scale=0.2)

        # Titles
        fps, frame_time = self.get_fps_and_frame_time()
        name, device, memory = self.get_device_and_memory()
        # glfw.set_window_title(self.window, self.window_title.format(FPS=fps)) # might confuse window managers
        self.static.fps = fps
        self.static.frame_time = frame_time
        self.static.name = name
        self.static.device = device
        self.static.memory = memory

        # Being the main window
        imgui.begin(f'{self.W}x{self.H} {fps:.3f} fps###main', flags=imgui.WindowFlags_.menu_bar)

        self.draw_menu_gui()
        self.draw_banner_gui()
        self.draw_camera_gui()
        self.draw_rendering_gui()
        self.draw_keyframes_gui()
        # self.draw_model_gui()
        self.draw_mesh_gui()
        self.draw_debug_gui()

        # End of main window and rendering
        imgui.end()

        imgui.pop_font()
        imgui.render()
        imgui.backends.opengl3_render_draw_data(imgui.get_draw_data())


async def websocket_client():
    global image
    global viewer
    async with websockets.connect(uri) as websocket:

        while True:
            buffer = await websocket.recv()
            buffer = decode_jpeg(torch.from_numpy(np.frombuffer(buffer, np.uint8)), device='cuda')
            with lock:
                image = buffer

            camera = deepcopy(viewer.camera)
            camera_data = zlib.compress(camera.to_string().encode('ascii'))
            await websocket.send(camera_data)

uri = "ws://10.76.5.252:1024"
image = None
lock = threading.Lock()
viewer = Viewer()


def start_client():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    loop.run_until_complete(websocket_client())
    loop.run_forever()


client_thread = threading.Thread(target=start_client, daemon=True)
client_thread.start()
catch_throw(viewer.run)()
