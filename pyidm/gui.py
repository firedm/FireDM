"""
    PyIDM

    multi-connections internet download manager, based on "pyCuRL/curl", "youtube_dl", and "PySimpleGUI"

    :copyright: (c) 2019-2020 by Mahmoud Elshahat.
    :license: GNU LGPLv3, see LICENSE for more details.
"""
import gc
import sys
import webbrowser
from queue import Queue

import PySimpleGUI as sg
import tkinter.font
import os
import time
import copy
from threading import Thread, Lock
from collections import deque

from .utils import *
from . import setting
from . import config
from .config import Status
from . import update
from .brain import brain
from . import video
from .video import Video, check_ffmpeg, download_ffmpeg, unzip_ffmpeg, get_ytdl_options, process_video_info, \
    download_m3u8, parse_subtitles, download_sub
from .downloaditem import DownloadItem
from .iconsbase64 import *

# imports for systray icon
try:
    import pystray
    from PIL import Image
    import io
    import base64
except Exception as e:
    log('Warning!! "pystray" package is required for systray icon, import error', e, log_level=2)

# todo: this module needs some clean up

# gui Settings
default_font = 'Helvetica 10'  # Helvetica font is guaranteed to work on all operating systems
config.all_themes = natural_sort(sg.ListOfLookAndFeelValues())
sg.SetOptions(icon=APP_ICON, font=default_font, auto_size_buttons=True, progress_meter_border_depth=0,
              border_width=1)

# transparent color for button which mimic current background, will be used as a parameter, ex. **transparent
transparent = {}


class MainWindow:
    def __init__(self):
        """This is the main application user interface window"""

        self.active = True  # active flag, if true this window will run in main application loop in "pyIDM.py"

        # current download_item
        self.d = DownloadItem()

        # main window
        self.window = None

        # active child windows
        self.active_windows = []  # list holds active_Windows objects

        # url
        self.url = ''  # current url in url input widget
        self.url_timer = 0
        self.bad_headers = [0, range(400, 404), range(405, 418), range(500, 506)]  # response codes

        # playlist/video
        self.video = None
        self.yt_id = 0  # unique id for each youtube thread
        self.playlist = []
        self.pl_title = ''
        self.pl_quality = None
        self._pl_menu = []
        self._stream_menu = []
        self.m_bar_lock = Lock()  # a lock to access a video quality progress bar from threads
        self._m_bar = 0  # main playlist progress par value
        self._s_bar = 0  # individual video streams progress bar value
        self.requested_quality = None  # it will be used when refresh link pressed to select the same quality for selected item

        # download
        self.pending = deque()  # todo: use normal queue

        # download list
        self.d_headers = ['i', 'rendered_name', 'progress', 'speed', 'time_left', 'downloaded', 'total_size', 'status']
        self.d_list = []  # list of DownloadItem() objects
        self.selected_row_num = None
        self._selected_d = None
        self.last_table_values = []  # download items table

        # thumbnail
        self.current_thumbnail = None

        # timers
        self.statusbar_timer = 0
        self.timer1 = 0
        self.one_time = True
        self.check_for_update_timer = time.time() - 55  # run interval is 60 seconds, use 'time.time() - 55' to make first start after 5 seconds

        self.total_speed = ''

        # initial setup
        self.setup()

    @staticmethod
    def startup():
        # start log recorder
        Thread(target=log_recorder, daemon=True).start()

        log('-' * 50, 'PyIDM', '-' * 50)
        log('Starting PyIDM version:', config.APP_VERSION, 'Frozen' if config.FROZEN else 'Non-Frozen')
        log('operating system:', config.operating_system_info)
        log('Python version:', sys.version)
        log('PySimpleGUI:', sg.version)

        log('current working directory:', config.current_directory)
        os.chdir(config.current_directory)

        if video.ytdl:
            log('youtube-dl ready')

    def setup(self):
        """initial setup"""

        # empty main window's queue and log queue
        # for q in (config.main_window_q, config.log_q):
        #     reset_queue(q)

        # startup
        self.startup()

        # load stored setting from disk
        setting.load_setting()
        config.d_list = setting.load_d_list()

        # update d_list
        self.d_list = config.d_list  # list of DownloadItem() objects

        # set global theme
        self.select_theme()

        # override PySimpleGUI standard buttons' background with artwork using a decorator function
        def button_decorator(init_func):
            def new_init_func(btn, *args, **kwargs):
                # pre processing arguments
                if not kwargs.get('font'):
                    button_text = kwargs.get('button_text') or args[0] or ' '
                    # adjust font to let text fit within button bg image boundaries 66x25 pixels
                    if len(button_text) < 6:
                        kwargs['font'] = 'any 10 bold'
                    elif len(button_text) < 7:
                        kwargs['font'] = 'any 9 bold'
                    else:
                        kwargs['font'] = 'any 8 bold'

                # calling Button __init__() constructor
                init_func(btn, *args, **kwargs)

                # post process Button properties
                if not btn.ImageData:
                    btn.ImageData = default_button_icon
                    btn.ButtonColor = ('black', sg.theme_background_color())
                    btn.BorderWidth = 0

            return new_init_func

        # redefine Button constructor
        if not config.operating_system == 'Darwin':  # disabled for mac
            sg.Button.__init__ = button_decorator(sg.Button.__init__)

        # download folder
        if not self.d.folder:
            self.d.folder = config.download_folder

        # main window
        self.start_window()

        self.reset()
        self.reset_video_controls()

        # update table one time at least
        self.update_table()

    def read_q(self):
        # read incoming messages from queue
        for _ in range(config.main_window_q.qsize()):
            if not self.active:
                return
            k, v = config.main_window_q.get()
            if k == 'url':
                self.window['url'](v.strip())
                self.on_url_text_change()

            elif k == 'download':  # todo: tobe removed
                self.start_download(*v)

            elif k == 'popup':
                type_ = v['type_']
                if type_ == 'popup_no_buttons':
                    sg.popup_no_buttons(v['msg'], title=v['title'])
                else:
                    sg.popup(v['msg'], title=v['title'])

        # read commands coming from other threads / modules and execute them.
        for _ in range(config.commands_q.qsize()):
            try:
                command = ''
                command, args, kwargs = config.commands_q.get()
                log(f'MainWindow, received command: {command}(), args={args}, kwargs={kwargs}', log_level=3)
                getattr(self, command)(*args, **kwargs)
            except Exception as e:
                log('MainWindow, error running command:', command, e, log_level=3)

    # region gui design

    def create_main_tab(self):
        # get current bg and text colors
        bg_color = sg.theme_background_color()
        text_color = sg.theme_text_color() if sg.theme_text_color() != "1234567890" else 'black'

        # column for playlist menu
        video_block = sg.Col([
                              [sg.Combo(values=self.pl_menu, size=(36, 1), key='pl_menu', enable_events=True, pad=(0, 5))],
                              [sg.Combo(values=self.stream_menu, size=(36, 1), key='stream_menu', enable_events=True, pad=(0, 5))],
                              [sg.ProgressBar(max_value=100, size=(15, 9), key='m_bar', pad=(0, 5)),
                               sg.ProgressBar(max_value=100, size=(5, 9), key='s_bar', pad=(0, 5))]], size=(290, 80))

        layout = [
            # spacer
            [sg.T('', font='any 2')],

            # app icon and app name
            [sg.Image(data=APP_ICON, enable_events=True, key='app_icon'),
             sg.Text(f'{config.APP_NAME}', font='any 20', justification='center', key='app_name', enable_events=True),
             ],

            # url entry
            [sg.T('Link:  '),
            sg.Input(self.d.url, enable_events=True, key='url', size=(49, 1),  right_click_menu=['url', ['copy url', 'paste url']]),
            sg.Button('', key='Retry', tooltip=' retry ', image_data=refresh_icon, **transparent)],

            # playlist/video block
            [sg.Col([[sg.T('       '), sg.Image(data=thumbnail_icon, key='main_thumbnail', enable_events=True, tooltip=' properties ')]],
                    size=(320, 110)),
             sg.Frame('Playlist/video:',
                      [[video_block]],
                      relief=sg.RELIEF_SUNKEN, key='playlist_frame'),
             sg.Column([
                 [sg.T('', font='any 2')],
                 [sg.Button('', tooltip=' download playlist ', key='pl_download', image_data=playlist_icon, **transparent)],
                 [sg.T('', font='any 2')],
                 [sg.Button('', tooltip=' subtitles ', key='subtitles', image_data=subtitle_icon, **transparent)]],
             pad=(0,0))
             ],

            # format id
            [sg.T(' ' * 300, key='format_id', font='any 8', pad=(5, 0))],

            # folder
            [sg.Image(data=folder_icon),
             sg.Input(config.download_folder, size=(55, 1), key='folder', enable_events=True, background_color=bg_color,
                      text_color=text_color, ),
             sg.B('', image_data=browse_icon, **transparent, key='browse',
                  button_type=sg.BUTTON_TYPE_BROWSE_FOLDER, target='folder')],

            # file name
            [sg.Text('File:', pad=(6, 0)),
             sg.Input('', size=(65, 1), key='name', enable_events=True, background_color=bg_color,
                      text_color=text_color), sg.Text('      ')],

            # file properties
            [sg.T('-' * 100, key='file_properties', font='any 9'),
             sg.T('', key='critical_settings_warning', visible=False, font='any 9', size=(30, 1))],

            # download button
            [sg.Column([[sg.B('', image_data=download_icon, key='Download', **transparent)]],
                       size=(166, 52), justification='center')],

        ]

        return layout

    def create_downloads_tab(self):
        table_right_click_menu = ['Table', ['!Options for selected file:', '---', 'Open File', 'Open File Location',
                                            '▶ Watch while downloading', 'copy webpage url', 'copy direct url',
                                            'copy playlist url', '⏳ Schedule download', '⏳ Cancel schedule!',
                                            'properties']]

        # buttons
        resume_btn = sg.Button('', key='Resume', tooltip=' Resume ', image_data=resume_icon, **transparent)
        stop_btn = sg.Button('', key='Cancel', tooltip=' Stop (Esc) ', image_data=stop_icon, **transparent)
        refresh_btn = sg.Button('', key='Refresh', tooltip=' Refresh link ', image_data=refresh_icon, **transparent)
        folder_btn = sg.Button('', key='Folder', tooltip=' open download folder ', image_data=folder_icon, **transparent)
        sched_btn = sg.B('', key='schedule_item', tooltip=' Schedule current item ', image_data=sched_icon, **transparent)
        del_btn = sg.Button('', key='delete_btn', tooltip=' Delete item from list (Del) ', image_data=delete_icon, **transparent)

        resume_all_btn = sg.Button('', key='Resume All', tooltip=' Resume All ', image_data=resumeall_icon, **transparent)
        stop_all_btn = sg.Button('', key='Stop All', tooltip=' Stop All ', image_data=stopall_icon, **transparent)
        sched_all_btn = sg.B('', key='Schedule All', tooltip=' Schedule All ', image_data=sched_icon, **transparent)
        del_all_btn = sg.Button('', key='delete_all', tooltip=' Delete All items from list ', image_data=deleteall_icon, **transparent)

        # selected download item's preview panel, "si" = selected item
        si_layout = [sg.Image(data=thumbnail_icon, key='si_thumbnail', right_click_menu=table_right_click_menu,
                              enable_events=True, tooltip=' Play '),
                     sg.Col([[sg.T('', size=(75, 5), key='si_out', font='any 9', enable_events=True, tooltip=' more! ')],
                            [sg.ProgressBar(100, size=(20, 10), key='si_bar'), sg.T(' ', size=(7, 1), key='si_percent'),
                             # *[copy.copy(x) for x in (resume_btn, stop_btn, folder_btn)],
                             ]])]

        # for table
        headings = ['i', 'name', '%', 'speed', 'left', 'done', 'size', 'status']
        col_widths = [6, 30, 10, 10, 10, 10, 10, 10]

        layout = [
            [
                resume_btn, stop_btn, refresh_btn, folder_btn, sched_btn, del_btn,

                sg.T(' ', size=(30, 1)),

                sg.Column([[resume_all_btn, stop_all_btn, sched_all_btn, del_all_btn]], key='master_buttons',
                          pad=(0, 0), visible=True),

            ],

            # table
            [sg.Table(values=[], headings=headings, num_rows=9, justification='left', auto_size_columns=False,
                      vertical_scroll_only=False, key='table', enable_events=False, font='any 9',
                      right_click_menu=table_right_click_menu, max_col_width=100, col_widths=col_widths,
                      row_height=22,
                      )],  # don't enable events for table, there is some bindings in self.start_window()

            si_layout
        ]

        return layout

    def create_settings_tab(self):
        """settings tab with TabGroup"""

        proxy_tooltip = """proxy setting examples:
                - http://proxy_address:port
                - 157.245.224.29:3128

                or if authentication required: 
                - http://username:password@proxyserveraddress:port  

                then choose proxy type i.e. "http, https, socks4, or socks5"  
                """

        general = [
            [sg.T('', size=(60, 1)), sg.Button('About', key='about', pad=(5, 10))],

            [sg.T('Settings Folder:'),
             sg.Combo(values=['Local', 'Global'],
                      default_value='Local' if config.sett_folder == config.current_directory else 'Global',
                      key='sett_folder', enable_events=True),
             sg.T(config.sett_folder, key='sett_folder_text', size=(100, 1), font='any 9')],

            [sg.Text('Select Theme:  '),
             sg.Combo(values=config.all_themes, default_value=config.current_theme, size=(15, 1),
                      enable_events=True, key='themes'),
             sg.Text(f' Total: {len(config.all_themes)} Themes'),
             sg.Text(' '*5),
             sg.Checkbox('Instant Theme change!', default=config.dynamic_theme_change, key='dynamic_theme_change',
                         enable_events=True, tooltip=' Experimental: Change theme instantly without restart ')
             ],

            [sg.Checkbox('Monitor copied urls in clipboard', default=config.monitor_clipboard,
                         key='monitor', enable_events=True)],

            [sg.Checkbox("Show download window", key='show_download_window',
                         default=config.show_download_window, enable_events=True)],
            [sg.Checkbox("Auto close download window after finish downloading", key='auto_close_download_window',
                         default=config.auto_close_download_window, enable_events=True)],

            [sg.Checkbox("Show video Thumbnail", key='show_thumbnail', default=config.show_thumbnail,
                         enable_events=True)],

            # [sg.Text('Segment size:  '), sg.Input(default_text=size_format(config.segment_size), size=(10, 1),
            #                                       enable_events=True, key='segment_size'),
            #  sg.Text(f'Current value: {size_format(config.segment_size)}', size=(30, 1), key='seg_current_value'),
            #  sg.T('*ex: 512 KB or 5 MB', font='any 8')],

            [sg.Checkbox('Playlist: Fetch all videos info in advance - *not recommended!!* -', default=config.process_playlist,
                         enable_events=True, key='process_playlist')],

            [sg.Checkbox('Manually select audio format for dash videos', default=config.manually_select_dash_audio,
                         enable_events=True, key='manually_select_dash_audio')],

            [sg.Checkbox('Auto rename file if same name exists in download folder', default=config.auto_rename,
                         enable_events=True, key='auto_rename')],
            [sg.Checkbox('Write metadata to media files', default=config.write_metadata,
                         enable_events=True, key='write_metadata')]
        ]

        network = [
            [sg.T('')],
            [sg.Checkbox('Speed Limit:', default=True if config.speed_limit else False,
                         key='speed_limit_switch', enable_events=True,
                         ),
             sg.Input(default_text=size_format(config.speed_limit) if config.speed_limit else '',
                      size=(10, 1), key='speed_limit',
                      disabled=False if config.speed_limit else True, enable_events=True),
             sg.T('0', size=(30, 1), key='current_speed_limit'),
             sg.T('*ex: 512 KB or 5 MB', font='any 8')],
            # [sg.T('', font='any 1')],  # spacer
            [sg.Text('Max concurrent downloads:      '),
             sg.Combo(values=[x for x in range(1, 101)], size=(5, 1), enable_events=True,
                      key='max_concurrent_downloads', default_value=config.max_concurrent_downloads)],
            [sg.Text('Max connections per download:'),
             sg.Combo(values=[x for x in range(1, 101)], size=(5, 1), enable_events=True,
                      key='max_connections', default_value=config.max_connections)],
            [sg.T('', font='any 1')],  # spacer
            [sg.Checkbox('Proxy:', default=config.enable_proxy, key='enable_proxy',
                         enable_events=True),
             sg.I(default_text=config.raw_proxy, size=(25, 1), font='any 9', key='raw_proxy',
                  enable_events=True, disabled=not config.enable_proxy),
             sg.T('?', tooltip=proxy_tooltip, pad=(3, 1)),
             sg.Combo(['http', 'https', 'socks4', 'socks5'], default_value=config.proxy_type,
                      font='any 9',
                      enable_events=True, key='proxy_type'),
             sg.T(config.proxy if config.proxy else '_no proxy_', key='current_proxy_value',
                  size=(100, 1), font='any 9'),
             ],
            [sg.Checkbox('Use proxy DNS', default=config.use_proxy_dns, key='use_proxy_dns', enable_events=True)],
            [sg.T('', font='any 1')],  # spacer

            [sg.Checkbox('Website Auth: ', default=config.use_web_auth, key='use_web_auth', enable_events=True),
             sg.T('    *user/pass will not be saved on disk', font='any 8')],
            [sg.T('        user: '),
             sg.I('', size=(25, 1), key='username', enable_events=True, disabled=not config.use_web_auth)],
            [sg.T('        Pass:'), sg.I('', size=(25, 1), key='password', enable_events=True,
                                         disabled=not config.use_web_auth, password_char='*')],
            [sg.T('', font='any 1')],  # spacer

            [sg.Checkbox('Referee url:   ', default=config.use_referer, key='use_referer', enable_events=True),
             sg.I(default_text=config.referer_url, size=(20, 1), font='any 9', key='referer_url',
                  enable_events=True, disabled=not config.use_referer)],

            [sg.Checkbox('Use Cookies:', default=config.use_cookies, key='use_cookies', enable_events=True),
             sg.I(default_text=config.cookie_file_path, size=(20, 1), font='any 9', key='cookie_file_path',
                  enable_events=True, disabled=not config.use_cookies), sg.FileBrowse('Browse')],


        ]

        systray = [
            [sg.T(' ', size=(100, 1))],
            [sg.T('SysTray:')],
            [sg.Frame(title='Action when closing Main Window:', layout=[
                [sg.T(' '), sg.Radio('Close App to systray', group_id='close_action', key='radio_close', enable_events=True),
                 sg.T('*Shutdown Main process, any activities / downloads will be cancelled', font='any 8')],

                [sg.T(' '), sg.Radio('Minimize App to systray', group_id='close_action', key='radio_minimize', enable_events=True),
                 sg.T('*Run in background, all activities / downloads will continue to run', font='any 8')],

                [sg.T(' '), sg.Radio('Quit (and close systray)', group_id='close_action', key='radio_quit', enable_events=True),
                 sg.T('*Shutdown down completely, including systray', font='any 8', size=(100, 1))],

            ])],

            [sg.T('', size=(1, 1))]
        ]

        update = [
            [sg.T(' ', size=(100, 1))],
            [sg.T('Check for update:'),
             sg.Combo(list(config.update_frequency_map.keys()), default_value=[k for k, v in config.update_frequency_map.items() if v == config.update_frequency][0],
                      size=(15, 1), key='update_frequency', enable_events=True)],
            [
                sg.B('', key='update_pyIDM', image_data=refresh_icon, **transparent, tooltip='check for update'),
                sg.T(f'PyIDM version = {config.APP_VERSION}', size=(50, 1), key='pyIDM_version_note'),
            ],
            [
                sg.B('', key='update_youtube_dl', image_data=refresh_icon, **transparent,
                     tooltip=' check for update '),
                sg.T('Youtube-dl version = 00.00.00', size=(50, 1), key='youtube_dl_update_note'),
                sg.B('', key='rollback_ytdl_update', image_data=delete_icon, **transparent,
                     tooltip=' rollback youtube-dl update '),
            ],
            [sg.T('', size=(1, 14))]  # fill lines
        ]

        advanced = [

            [sg.T('')],
            [sg.T('Developer options: "*you should know what you are doing before modifying these options!"')],
            [sg.Checkbox('keep temp files / folders after done downloading for debugging.',
                         default=config.keep_temp, key='keep_temp', enable_events=True, )],
            [sg.Checkbox('Re-raise all caught exceptions / errors for debugging "Application will crash on any Error"',
                         default=config.TEST_MODE, key='TEST_MODE', enable_events=True,)],
            [sg.Checkbox('Show "MD5 and SHA256" checksums for downloaded files in log',
                         default=config.checksum, key='checksum', enable_events=True, )],
            [sg.Checkbox('Use ThreadPoolExecutor instead of individual threads',
                         default=config.use_thread_pool_executor, key='use_thread_pool_executor', enable_events=True, )],
        ]

        # layout ----------------------------------------------------------------------------------------------------
        layout = [
            [sg.T('', size=(70, 1)), ],
            [sg.TabGroup([[sg.Tab('General   ', general), sg.Tab('Network  ', network), sg.Tab('SysTray  ', systray),
                           sg.Tab('Update    ', update), sg.Tab('Advanced ', advanced)]],
                         tab_location='lefttop')]
        ]

        return layout

    def create_window(self):
        # main tab layout
        main_layout = self.create_main_tab()

        # downloads tab -----------------------------------------------------------------------------------------
        downloads_layout = self.create_downloads_tab()

        # Settings tab -------------------------------------------------------------------------------------------
        settings_layout = self.create_settings_tab()

        # log tab ------------------------------------------------------------------------------------------------
        log_layout = [[sg.T('Details events:')],
                      [sg.Multiline(default_text='', size=(70, 22), key='log', font='any 8', autoscroll=True)],
                      [sg.T('')],

                      [sg.T('Log Level:'), sg.Combo([1, 2, 3], default_value=config.log_level, enable_events=True,
                                                    size=(3, 1), key='log_level',
                                                    tooltip='*(1=Standard, 2=Verbose, 3=Debugging)'),
                       sg.T(' ', size=(10, 1), key='log_spacer'),
                       # sg.Button('copy Log', key='copy_log', tooltip=' copy Log to clipboard '),
                       sg.Button('Log file', key='open_log_file', tooltip=' open Log file '),
                       sg.Button('Folder', key='open_log_folder', tooltip=' open Log folder '),
                       sg.Button('Clear', key='clear_log', tooltip=' Clear Log ')]]

        # status bar relief
        sb_relief = sg.RELIEF_SUNKEN
        layout = [[sg.TabGroup(
            [[sg.Tab('Main', main_layout), sg.Tab('Downloads', downloads_layout), sg.Tab('Settings', settings_layout),
              sg.Tab('Log', log_layout)]],
            key='tab_group')],
            [
             sg.T('', size=(73, 1), relief=sb_relief, font='any 8', key='status_bar'),
             sg.Text('', size=(10, 1), key='status_code', relief=sb_relief, font='any 8'),
             sg.T('5 ▼  |  6 ⏳', size=(12, 1), key='active_downloads', relief=sb_relief, font='any 8',
                  tooltip=' active downloads | pending downloads '),
             sg.T('⬇350 bytes/s', font='any 8', relief=sb_relief, size=(12, 1), key='total_speed'),
            ]
        ]

        # window
        window = sg.Window(title=config.APP_TITLE, layout=layout, size=config.window_size, margins=(2, 2),
                           return_keyboard_events=True, resizable=True)
        return window

    def start_window(self):
        self.active = True
        config.terminate = False

        self.window = self.create_window()
        self.window.Finalize()

        # override x button
        self.window.TKroot.protocol('WM_DELETE_WINDOW', self.close_callback)

        # expand elements to fit
        elements = ['url', 'name', 'folder', 'm_bar', 'pl_menu', 'file_properties', 'stream_menu', 'log',
                    'cookie_file_path', 'referer_url', 'log_spacer']  # elements to be expanded
        for element in elements:
            self.window[element].expand(expand_x=True)

        # bind keys events for table, it is tkinter specific
        self.window['table'].Widget.bind("<Button-3>", self.table_right_click)  # right click
        self.window['table'].Widget.bind("<ButtonRelease-1>", self.on_table_click)  # don't enable events for table
        self.window['table'].bind('<Double-Button-1>', '_double_clicked')  # will generate event "table_double_clicked"
        self.window['table'].bind('<Return>', '_enter_key')  # will generate event "table_enter_key"

        # change cursor for some widgets
        self.window['si_thumbnail'].set_cursor('hand2')
        self.window['main_thumbnail'].set_cursor('hand2')
        self.window['si_out'].set_cursor('hand2')

        # log text, disable word wrap
        # use "undo='false'" disable tkinter caching to fix issue #59 "solve huge memory usage and app crash
        self.window['log'].Widget.config(wrap='none', undo='false')

        # bind mouse wheel for ('pl_menu' and 'stream_menu') only combo boxes, the rest combos are better without it
        def handler1(event):
            # pl_menu_mouse_wheel_handler
            try:
                i = event.widget.current()

                i = i - 1 if event.delta > 0 else i + 1
                if 0 <= i < len(self.playlist):
                    event.widget.current(i)
                    self.playlist_on_choice()
            except Exception as e:
                log('playlist menu handler', e, log_level=3)

        def handler2(event):
            # stream_menu_mouse_wheel_handler
            try:
                i = event.widget.current()

                i = i - 1 if event.delta > 0 else i + 1
                if 0 <= i < len(self.video.stream_menu if self.video else ''):
                    event.widget.current(i)
                    self.stream_on_choice()
            except Exception as e:
                log('stream menu handler', e, log_level=3)

        def bind_mouse_wheel(combo, handler):
            # bind combobox to mousewheel
            self.window[combo].Widget.bind("<MouseWheel>", handler, add="+")  # for windows

            # for linux
            self.window[combo].Widget.bind("<ButtonPress-4>", handler, add="+")
            self.window[combo].Widget.bind("<ButtonPress-5>", handler, add="+")

        bind_mouse_wheel('pl_menu', handler1)
        bind_mouse_wheel('stream_menu', handler2)

        # systray radio buttons, assign default value
        self.window[f'radio_{config.close_action}'](True)

        # un hide active windows, if any
        self.un_hide_active_windows()

        # Horizontal scrollbar for Multiline ----------------------------------------------------
        multiline_widget = self.window['log'].Widget
        scrollbar_frame = self.window['log'].Widget.master.master.master

        s = sg.tk.Scrollbar(scrollbar_frame, orient='horizontal', command=multiline_widget.xview)
        # s.pack(side='bottom', anchor='w', fill='x', expand=True, padx=5)
        multiline_widget.config(xscrollcommand=s.set)
        s.place(in_=multiline_widget, relx=0, x=0, rely=1.0, y=0, width=660)

        # disable vertical autoscroll if user moves scrollbar up and enable when moved to bottom ------------------------
        vertical_scrollbar = multiline_widget.vbar

        self.window['log'].lastv = 1
        def vscroll(*args):
            multiline_widget.yview(*args)

            position = int(vertical_scrollbar.get()[-1])
            if position != self.window['log'].lastv:
                self.window['log'].lastv = position
                if position == 1:  # at the end/bottom
                    autoscroll = True
                else:
                    autoscroll = False
                
                self.window['log'](autoscroll=autoscroll)

        vertical_scrollbar.config(command=vscroll)

        # print tkinter version and patch ------------------------------------------------------------------------------
        root = self.window.TKroot
        log('tkinter version:', root.tk.call("info", "patchlevel"))

        # fix for table colors in tkinter 8.6.9 -----------------------------------------------------------------------------
        self.apply_theme_fix()

    def apply_theme_fix(self):
        # fix for table colors in tkinter 8.6.9
        style = sg.ttk.Style()
        def fixed_map(option):
            return [elm for elm in style.map('Treeview', query_opt=option) if '!' not in elm[0]]
        style.map('Treeview', foreground=fixed_map("foreground"), background=fixed_map("background"))

    def restart_window(self):
        try:
            # store log temporarily
            log = ''
            log = self.window['log'].get()

            self.window.Close()
        except:
            pass

        self.start_window()

        # restore log
        self.window['log'](log)

        if self.video:
            self.update_pl_menu()
            self.update_stream_menu()

            # get the last value of bars after restart
            self.m_bar = self._m_bar
            self.s_bar = self._s_bar

            # reset current thumbnail is required for show_thumbnail() to preview the last video thumbnail
            self.current_thumbnail = None
        else:
            self.pl_menu = ['Playlist']
            self.stream_menu = ['Video quality']

        # force update table
        self.update_table(force_update=True)

    def table_right_click(self, event):
        try:
            # select row under mouse
            id_ = self.window['table'].Widget.identify_row(event.y)  # first row = 1 not 0
            if id_:
                # mouse pointer over item
                self.window['table'].Widget.selection_set(id_)
                self.select_row(int(id_) - 1)  # get count start from zero
                self.window['table']._RightClickMenuCallback(event)
        except:
            pass

    def select_row(self, row_num):
        try:
            row_num = int(row_num)

            if self.selected_row_num != row_num:
                self.selected_row_num = row_num

                # update selected item in widget
                self.window['table'](select_rows=(self.selected_row_num,))

                # get instant gui update, don't wait for scheduled update
                self.update_gui()

        except Exception as e:
            log('MainWindow.select_row(): ', e)

    def select_tab(self, tab_name=''):
        try:
            self.window[tab_name].Select()
        except Exception as e:
            print(e)

    @property
    def active_tab(self):
        # notebook
        nb = self.window['tab_group'].Widget

        # return active tab name
        return nb.tab(nb.select(), "text")

    def update_log(self):
        """
        read config.log_q and display text in log tab
        :return: None
        """
        # update log
        # read 10 messages max every time to prevent application freeze, in case of error messages flood by ffmpeg
        for _ in range(min(100, config.log_q.qsize())):
            line = config.log_q.get()
            try:
                contents = self.window['log'].get()
                # print(size_format(len(contents)))
                if len(contents) > config.max_log_size:
                    # delete 20% of contents to keep size under max_log_size
                    slice_size = int(config.max_log_size * 0.2)
                    self.window['log'](contents[slice_size:])

                self.window['log'](line, append=True)
            except Exception as e:
                # print('MainWindow.read_q() log error', e)
                pass

            self.set_status(line.strip('\n'))

            # currently not implemented
            # parse youtube output while fetching playlist info with option "process=True"
            if '[download]' in line:  # "[download] Downloading video 3 of 30"
                try:
                    b = line.rsplit(maxsplit=3)  # ['[download] Downloading video', '3', 'of', '30']
                    total_num = int(b[-1])
                    num = int(b[-3])

                    # get 50% of this value and the remaining 50% will be for other processing
                    percent = int(num * 100 / total_num)
                    percent = percent // 2

                    # update media progress bar
                    self.m_bar = percent

                    # update playlist frame title
                    self.window['playlist_frame'](
                        value=f'Playlist ({num} of {total_num} {"videos" if num > 1 else "video"}):')
                except:
                    pass

    def on_table_click(self, event):
        selections = event.widget.selection()  # expected ('1', '2', '3', '4', '5', '6')

        # get selected rows starting from 0 and convert to int
        selections = [int(x) - 1 for x in selections]

        # we just use one selection in our application, will get the first one
        if selections:
            self.select_row(selections[0])

    def update_table(self, force_update=False):
        table_values = [[self.format_cell_data(key, getattr(d, key, '')) for key in self.d_headers] for d in
                        self.d_list]

        if self.last_table_values != table_values or force_update:
            # print('updated table')
            self.last_table_values = table_values
            self.window['table'](values=table_values[:])

            if self.d_list:
                # select first row by default if nothing previously selected
                if self.selected_row_num is None:
                    self.selected_row_num = 0
                    # print('self.selected_row_num', self.selected_row_num)

                # re-select the previously selected row in the table
                self.window['table'](select_rows=(self.selected_row_num,))

    def update_gui(self):
        """
        Periodically update gui widgets
        :return: None
        """
        if not self.active:
            return

        # handle url text change, time since last change only after 2 seconds, this prevent processing url with every
        # letter typed by user
        if 5 > time.time() - self.url_timer > 2:
            # set timer to negative value guarantee above condition to be false, i.e. execute one time only, 
            # until self.url_timer get reset when url text change
            self.url_timer = -10
            self.on_url_text_change()

        # update Elements
        try:
            self.update_log()

            # update status code widget
            self.window['status_code'](f'status: {self.d.status_code}')

            # file name
            if self.window['name'].get() != self.d.rendered_name:  # it will prevent cursor jump to end when modifying name
                self.window['name'](self.d.rendered_name)

            file_properties = f'Size: {size_format(self.d.total_size)} - Type: {self.d.type} - ' \
                              f'{", ".join(self.d.subtype_list)} - ' \
                              f'Protocol: {self.d.protocol} - Resumable: {"Yes" if self.d.resumable else "No"} ...'
            self.window['file_properties'](file_properties)

            # table
            if self.active_tab == 'Downloads':
                self.update_table()

            # update active and pending downloads
            self.window['active_downloads'](f' {len(self.active_downloads)} ▼  |  {len(self.pending)} ⏳')

            # Settings
            speed_limit = size_format(config.speed_limit) if config.speed_limit > 0 else "_no limit_"
            self.window['current_speed_limit'](f'Current value: {speed_limit}')

            self.window['youtube_dl_update_note'](
                f'Youtube-dl version = {config.ytdl_VERSION}, Latest version = {config.ytdl_LATEST_VERSION}')
            self.window['pyIDM_version_note'](
                f'PyIDM version = {config.APP_VERSION}, Latest version = {config.APP_LATEST_VERSION}')

            # update total speed
            total_speed = 0
            for i in self.active_downloads:
                d = self.d_list[i]
                total_speed += d.speed
            self.total_speed = f'⬇ {size_format(total_speed, "/s")}'
            self.window['total_speed'](self.total_speed)

            # thumbnail
            if self.video:
                if self.video.thumbnail:
                    self.show_thumbnail(thumbnail=self.video.thumbnail)
                else:
                    self.reset_thumbnail()

            # update selected download item's preview panel in downloads tab
            d = self.selected_d

            if d:
                speed = f"Speed: {size_format(d.speed, '/s') }  {time_format(d.time_left)} left"   # if d.speed else ''
                out = f"{self.selected_row_num + 1}- {self.fit_text(d.rendered_name, 75)}\n" \
                      f"Done: {size_format(d.downloaded)} of {size_format(d.total_size)}\n" \
                      f"{speed} \n" \
                      f"Live connections: {d.live_connections} - Remaining parts: {d.remaining_parts} - ({d.type}, {', '.join(d.subtype_list)}) \n" \
                      f"{d.status}  {d.i}"

                # thumbnail
                if config.show_thumbnail and d.thumbnail:
                    self.window['si_thumbnail'](data=d.thumbnail)
                else:
                    self.window['si_thumbnail'](data=thumbnail_icon)

            else:
                out = f"File:\n" \
                      f"Downloaded:\n" \
                      f"Speed: \n" \
                      f"Live connections: \n" \
                      f"Status:"
                self.window['si_thumbnail'](data=thumbnail_icon)

            self.window['si_out'](out)
            self.window['si_bar'].update_bar(d.progress if d else 0)
            self.window['si_percent'](f'{d.progress}%' if d else '')

            # animate side bar ------------------------------------------------------------------------
            if self.video:
                if self.d.busy:
                    self.s_bar = self.s_bar + 10 if self.s_bar < 90 else self.s_bar
                else:
                    self.s_bar = 100

            # update stream menu ----------------------------------------------------------------------
            if self.video and self.stream_menu != self.video.stream_menu:
                self.update_stream_menu()

            # critical_settings_warning: sometimes user set proxy or speed limit in settings and forget it is
            # already set, which affect the whole application operation, will show a flashing text at main Tab
            if config.proxy or config.speed_limit:
                proxy = 'proxy: active, ' if config.proxy else ''
                sl = f'Speed Limit: {size_format(config.speed_limit)}' if config.speed_limit else ''
                self.window['critical_settings_warning'](proxy + sl)
                flip_visibility(self.window['critical_settings_warning'])
            else:
                self.window['critical_settings_warning']('', visible=False)

        except Exception as e:
            if config.TEST_MODE:
                raise e
            log('MainWindow.update_gui() error:', e)

    def set_status(self, text):
        """update status bar text widget"""
        try:
            self.window['status_bar'](text)

            # reset timer, used to clear status bar
            self.statusbar_timer = time.time()
        except:
            pass

    def change_theme(self, theme_name):
        """
        change theme on the fly (dynamically) without a need to restart main window,
        :param theme_name: string represent theme name same parameter which passed to PySimpleGUI ChangeLookAndFeel()
        """

        # unfortunately PySimpleGui doesn't offer this functionality yet.
        # https://github.com/PySimpleGUI/PySimpleGUI/issues/2437
        # there is a known theme bug with tkinter 8.6.9 shipped with python 3.7.4+ affecting TreeView "Table",
        # bug url: https://core.tcl-lang.org/tk/info/509cafafae

        # set global theme for new windows
        self.select_theme(theme_name)

        # Modify theme for current window ................................................................
        # get theme dict
        theme = sg.LOOK_AND_FEEL_TABLE[theme_name]

        # get correct values for default theme
        default_bg = self.window.TKroot.config('bg')[-2]
        theme['BACKGROUND'] = theme['BACKGROUND'] if theme['BACKGROUND'] != '1234567890' else default_bg

        default_text_color = self.window['file_properties'].Widget.config('fg')[-2]
        theme['TEXT'] = theme['TEXT'] if theme['TEXT'] != '1234567890' else default_text_color

        default_input_color = self.window['url'].Widget.config('fg')[-2]
        theme['TEXT_INPUT'] = theme['TEXT_INPUT'] if theme['TEXT_INPUT'] != '1234567890' else default_input_color

        default_input_bg = self.window['url'].Widget.config('bg')[-2]
        theme['INPUT'] = theme['INPUT'] if theme['INPUT'] != '1234567890' else default_input_bg

        # Set main window background
        self.window.TKroot.config(bg=theme['BACKGROUND'])

        # create style object
        style = sg.ttk.Style()

        # set theme for individual widgets
        def set_widget_theme(widgets_dict):
            """
            set theme for individual widgets
            :param widgets_dict: dictionary with name: widget as an output of tkinter_widget.children
            :return: None
            """

            for widget_name, widget in widgets_dict.items():
                # use widget.winfo_class() which is better than isinstance() in this case because we need the direct
                # class type/ name, with "isinstance" we can't differentiate between tk.Entry and tk.ComboBox because combobox
                # is subclassed from "Entry"
                # widget.winfo_class() examples of a return name:
                # {'Treeview', 'Scrollbar', 'Checkbutton', 'Text', 'Menu', 'Labelframe', 'Label', 'Button', 'Canvas',
                # 'TCombobox', 'Radiobutton', 'Frame', 'TProgressbar', 'Entry', 'TNotebook'}
                # print(widget_name, ':', type(widget))

                try:
                    if widget.winfo_class() == 'Labelframe':
                        widget.config(fg=theme['TEXT'], bg=theme['BACKGROUND'])

                    elif widget.winfo_class() == 'Frame':
                        widget.config(bg=theme['BACKGROUND'])

                    elif widget.winfo_class() == 'Label':
                        widget.config(fg=theme['TEXT'], bg=theme['BACKGROUND'])

                    elif widget.winfo_class() == 'Checkbutton':
                        fg = theme['TEXT']
                        bg = theme['BACKGROUND']

                        widget.config(fg=fg, bg=bg, activebackground=bg, selectcolor=bg)

                    elif widget.winfo_class() == 'Entry' or widget.winfo_class() == 'Text':  # note: Text == tk.scrolledtext.ScrolledText:
                        widget.config(fg=theme['TEXT_INPUT'], bg=theme['INPUT'])

                    elif widget.winfo_class() == 'TProgressbar':
                        BarColor = theme['PROGRESS']
                        style.configure("my.Horizontal.TProgressbar", background=BarColor[0], troughcolor=BarColor[1],
                                    borderwidth=0, thickness=9)  # troughrelief=relief
                        widget.config(style="my.Horizontal.TProgressbar")

                    elif widget.winfo_class() == 'TCombobox':
                        style_name = 'combo.TCombobox'
                        fg = theme['TEXT_INPUT']
                        bg = theme['INPUT']

                        style.configure(style_name, foreground=fg, selectforeground=fg, selectbackground=bg, fieldbackground=bg)
                        widget.config(style=style_name)

                    elif widget.winfo_class() == 'Radiobutton':
                        fg = theme['TEXT']
                        bg = theme['BACKGROUND']
                        widget.config(fg=fg, selectcolor=bg, bg=bg, activebackground=fg)

                    elif widget.winfo_class() == 'TNotebook':
                        # notebook colors
                        fg = theme['TEXT']
                        bg = theme['BACKGROUND']

                        # tabs colors
                        tab_fg = theme['TEXT_INPUT']
                        tab_bg = theme['INPUT']
                        selected_tab_fg = theme['TEXT']
                        selected_tab_bg = theme['BACKGROUND']

                        custom_style = widget.tab(0, "text").strip() + '.TNotebook'
                        tabposition = 'nw' if widget.tab(0, "text").strip() == 'Main' else 'wn'

                        style.configure(custom_style, foreground=fg, background=bg, tabposition=tabposition, font=default_font)
                        style.configure(custom_style + '.Tab', background=tab_bg, foreground=tab_fg, font=default_font)
                        style.map(custom_style + '.Tab', foreground=[("selected", selected_tab_fg)], background=[("selected", selected_tab_bg)])
                        widget.config(style=custom_style)

                    elif widget.winfo_class() == 'Treeview':  # our table in Downloads Tab

                        fg = theme['TEXT']
                        bg = theme['BACKGROUND']

                        heading_fg = theme['TEXT_INPUT']
                        heading_bg = theme['INPUT']

                        custom_style = 'my.Treeview'

                        style.configure(custom_style, foreground=fg, background=bg, fieldbackground=bg, font='any 9', rowheight=22)
                        style.configure(custom_style +'.Heading', foreground=heading_fg, background=heading_bg)
                        widget.config(style=custom_style)

                        # required for future added rows to the table
                        table = self.window['table']
                        table.BackgroundColor = bg
                        table.TextColor = fg

                        # rows
                        for row in range(len(self.d_list)):
                            try:
                                widget.tag_configure(row, foreground=fg, background=bg)
                            except:
                                pass

                    elif widget.winfo_class() in ('Button', 'TButton'):
                        # make our button transparent
                        widget.config(bg=theme['BACKGROUND'])

                    else:
                        # anything else
                        widget.config(bg=theme['BACKGROUND'])

                except Exception as e:
                    log(widget.winfo_class(), ':', repr(widget), e, log_level=3)

                # recursive call to handle children widgets
                if hasattr(widget, 'children'):
                    children = widget.children  # a dictionary of names vs widgets
                    set_widget_theme(children)

        # change theme for all children in main window
        set_widget_theme(self.window.TKroot.children)

        # special requirements
        # 2 Input / Entry in main tab should have lable theme
        self.window['name'].Widget.config(fg=theme['TEXT'], bg=theme['BACKGROUND'])
        self.window['folder'].Widget.config(fg=theme['TEXT'], bg=theme['BACKGROUND'])

    def select_theme(self, theme_name=None):
        # theme
        sg.ChangeLookAndFeel(theme_name or config.current_theme)

        # transparent color for button which mimic current background, will be use as a parameter, ex. **transparent
        global transparent
        transparent = dict(button_color=('black', sg.theme_background_color()), border_width=0)

    def fit_text(self, text, req_width):  # todo: replace all truncate usage with this method
        """
        truncate a text to a required width
        :param text: text to be truncated
        :param req_width: int, the required text width in characters
        :return: truncated text
        """

        # create tkinter font object, must pass a root window
        font = tkinter.font.Font(root=self.window.TKroot.master, font=default_font)

        # convert width in characters to width in pixels, tkinter uses '0' zero character as a default unit
        req_width = font.measure('0' * req_width)  # convert to pixels

        # measure text in pixels
        text_width = font.measure(text)

        if text_width <= req_width:
            return text

        # iterate and uses less character count until we get the target width
        length = len(text)
        while True:
            # will truncate text from the middle, see utils.truncate() for more info
            processed_text = truncate(text, length)
            if font.measure(processed_text) <= req_width or length <= 0:
                return processed_text

            length -= 1

    def show_properties(self, d):
        """
        Display properties of download item in a popup window
        :param d: DownloadItem object
        :return: None
        """
        try:

            if d:
                # General properties
                text = f'Name: {d.name} \n' \
                       f'Folder: {d.folder} \n' \
                       f'Progress: {d.progress}% \n' \
                       f'Downloaded: {size_format(d.downloaded)} \n' \
                       f'Total size: {size_format(d.total_size)} \n' \
                       f'Status: {d.status} \n' \
                       f'Resumable: {d.resumable} \n' \
                       f'Type: {d.type} - subtype: {", ".join(d.subtype_list)}\n'

                if d.type == 'video':
                    text += f'Protocol: {d.protocol} \n' \
                            f'Video quality: {d.selected_quality}\n' \
                            f'Audio quality: {d.audio_quality}\n\n' \
                            f'Webpage url: {d.url}\n\n' \
                            f'Playlist title: {d.playlist_title}\n' \
                            f'Playlist url: {d.playlist_url}\n\n' \
                            f'Direct video url: {d.eff_url}\n\n' \
                            f'Direct audio url: {d.audio_url}\n\n'
                else:
                    text += f'Webpage url: {d.url}\n\n' \
                            f'Direct url: {d.eff_url}\n\n'

                sg.popup_scrolled(text, title='Download Item properties', size=(50, 20), non_blocking=True)
        except Exception as e:
            log('gui> properties>', e)

    def show_download_window(self, d):
        if d not in [win.d for win in self.active_windows]:
            self.active_windows.append(DownloadWindow(d))
        else:
            win = [win for win in self.active_windows if win.d == d][0]
            win.focus()

    # endregion

    def run(self):
        """main loop"""

        try:
            # todo: we could use callback style for some of these if's
            event, values = self.window.Read(timeout=50)
            if event and event not in ('__TIMEOUT__'):
                log(event, log_level=4)

            if event is None:
                # close or hide active windows
                if config.close_action == 'minimize':
                    self.hide()
                else:
                    self.close()

            # keyboard events --------------------------------------------------
            elif event.startswith('Up:'):  # up arrow example "Up:38"
                # get current element with focus
                focused_elem = self.window.find_element_with_focus()

                # for table, change selected row
                if self.window['table'] == focused_elem and self.selected_row_num > 0:
                    self.select_row(self.selected_row_num - 1)

            elif event.startswith('Down:'):  # down arrow example "Down:40"
                # get current element with focus
                focused_elem = self.window.find_element_with_focus()

                # for table, change selected row
                if self.window['table'] == focused_elem and self.selected_row_num < len(self.window['table'].Values)-1:
                    self.select_row(self.selected_row_num + 1)

            elif event.startswith('Delete:'):  # Delete:46
                # get current element with focus
                focused_elem = self.window.find_element_with_focus()

                # for table, change selected row
                if self.window['table'] == focused_elem and self.selected_d:
                    self.delete_btn()

            elif event.startswith('Escape:'):  # Escape:27

                # get current element with focus

                focused_elem = self.window.find_element_with_focus()

                # for table, change selected row

                if self.window['table'] == focused_elem and self.selected_d:
                    self.cancel_btn()

            # Mouse events MouseWheel:Up, MouseWheel:Down -----------------------
            elif event == 'MouseWheel:Up':
                pass
            elif event == 'MouseWheel:Down':
                pass

            # Main Tab ----------------------------------------------------------------------------------------

            elif event == 'url':
                # reset timer, and self.url_text_change() will be called from update_gui()
                self.url_timer = time.time()

            elif event == 'copy url':
                url = values['url']
                if url:
                    clipboard.copy(url)

            elif event == 'paste url':
                self.window['url'](clipboard.paste().strip())
                self.on_url_text_change()

            # video events
            elif event == 'main_thumbnail':
                self.show_properties(self.d)

            elif event == 'pl_download':
                self.download_playlist()

            elif event == 'pl_menu':
                self.playlist_on_choice()

            elif event == 'stream_menu':
                self.stream_on_choice()

            elif event == 'subtitles':
                try:
                    self.download_subtitles()
                except Exception as e:
                    log('download_subtitles()> error', e)

            elif event == 'Download':
                self.download_btn()

            elif event == 'folder':
                if values['folder']:
                    config.download_folder = os.path.abspath(values['folder'])
                else:  # in case of empty entries
                    self.window['folder'](config.download_folder)

            elif event == 'name':
                self.d.name = validate_file_name(values['name'])

            elif event == 'Retry':
                self.retry()

            # downloads tab events -----------------------------------------------------------------------------------
            elif event in ('table_double_clicked', 'table_enter_key', 'Open File', '▶ Watch while downloading',
                           'si_thumbnail') and self.selected_d:
                if self.selected_d.status == Status.completed:
                    open_file(self.selected_d.target_file)
                else:
                    open_file(self.selected_d.temp_file)

            # table right click menu event
            elif event == 'copy webpage url':
                clipboard.copy(self.selected_d.url)

            elif event == 'copy direct url':
                clipboard.copy(self.selected_d.eff_url)

            elif event == 'copy playlist url':
                clipboard.copy(self.selected_d.playlist_url)

            elif event == 'properties':
                # right click properties
                self.show_properties(self.selected_d)

            elif event in ('⏳ Schedule download', 'schedule_item'):
                # print('schedule clicked')
                response = self.ask_for_sched_time(msg=self.selected_d.name)
                if response:
                    self.selected_d.sched = response

            elif event == '⏳ Cancel schedule!':
                self.selected_d.sched = None

            elif event == 'Resume':
                self.resume_btn()

            elif event == 'Cancel':
                self.cancel_btn()

            elif event == 'Refresh':
                self.refresh_link_btn()

            elif event in ('Folder', 'Open File Location'):
                if self.selected_d:
                    open_folder(self.selected_d.target_file)

            elif event in ('D.Window', 'si_out'):
                # create or show download window
                if self.selected_d:
                    if self.selected_d.status != Status.downloading:
                        self.show_properties(self.selected_d)
                    else:
                        self.show_download_window(self.selected_d)

            elif event == 'Resume All':
                self.resume_all_downloads()

            elif event == 'Stop All':
                self.stop_all_downloads()

            elif event == 'Schedule All':
                response = self.ask_for_sched_time(msg='Schedule all non completed files')
                if response:
                    for d in self.d_list:
                        if d.status in (Status.pending, Status.cancelled):
                            d.sched = response

            elif event == 'delete_btn':
                self.delete_btn()

            elif event == 'delete_all':
                self.delete_all_downloads()

            # Settings tab -------------------------------------------------------------------------------------------
            elif event in ('about', 'app_icon', 'app_name'):  # about window
                # check if "about_window" is already opened
                found = [window for window in self.active_windows if isinstance(window, AboutWindow)]
                if found:
                    about_window = found[0]

                    # bring window to front
                    about_window.focus()

                else:  # not found
                    # create new window and append it to active windows
                    about_window = AboutWindow()
                    self.active_windows.append(about_window)

            elif event == 'themes':
                config.current_theme = values['themes']

                if config.dynamic_theme_change:
                    self.change_theme(config.current_theme)
                else:
                    self.select_theme()

                    # close all active windows
                    for win in self.active_windows:
                        win.window.Close()
                    self.active_windows.clear()

                    self.restart_window()
                    self.select_tab('Settings')

            elif event == 'dynamic_theme_change':
                config.dynamic_theme_change = values['dynamic_theme_change']

            elif event == 'show_thumbnail':
                config.show_thumbnail = values['show_thumbnail']

                self.reset_thumbnail()

            elif event == 'monitor':
                config.monitor_clipboard = values['monitor']

            elif event == 'show_download_window':
                config.show_download_window = values['show_download_window']

            elif event == 'auto_close_download_window':
                config.auto_close_download_window = values['auto_close_download_window']

            elif event == 'process_playlist':
                config.process_playlist = values['process_playlist']

            elif event == 'manually_select_dash_audio':
                config.manually_select_dash_audio = values['manually_select_dash_audio']

            elif event == 'auto_rename':
                config.auto_rename = values['auto_rename']

            elif event == 'write_metadata':
                config.write_metadata = values['write_metadata']

            # elif event == 'segment_size':
            #     user_input = values['segment_size']
            #
            #     # if no units entered will assume it KB
            #     try:
            #         _ = int(user_input)  # will succeed if it has no string
            #         user_input = f'{user_input} KB'
            #     except:
            #         pass
            #
            #     seg_size = parse_bytes(user_input)
            #
            #     # set non valid values or zero to default
            #     if not seg_size:
            #         seg_size = config.DEFAULT_SEGMENT_SIZE
            #
            #     config.segment_size = seg_size
            #     self.window['seg_current_value'](f'current value: {size_format(config.segment_size)}')
            #     self.d.segment_size = seg_size

            elif event == 'sett_folder':
                selected = values['sett_folder']
                if selected == 'Local':
                    # choose local folder as a Settings folder
                    config.sett_folder = config.current_directory

                    # remove setting.cfg from global folder
                    delete_file(os.path.join(config.global_sett_folder, 'setting.cfg'))
                else:
                    # choose global folder as a setting folder
                    config.sett_folder = config.global_sett_folder

                    # remove setting.cfg from local folder
                    delete_file(os.path.join(config.current_directory, 'setting.cfg'))

                    # create global folder settings if it doesn't exist
                    if not os.path.isdir(config.global_sett_folder):
                        try:
                            choice = sg.popup_ok_cancel(f'folder: {config.global_sett_folder}\n'
                                                        f'will be created')
                            if choice != 'OK':
                                raise Exception('Operation Cancelled by User')
                            else:
                                os.mkdir(config.global_sett_folder)

                        except Exception as e:
                            log('global setting folder error:', e)
                            config.sett_folder = config.current_directory
                            sg.popup(f'Error while creating global settings folder\n'
                                     f'"{config.global_sett_folder}"\n'
                                     f'{str(e)}\n'
                                     f'local folder will be used instead')
                            self.window['sett_folder']('Local')
                            self.window['sett_folder_text'](config.sett_folder)

                # update display widget
                try:
                    self.window['sett_folder_text'](config.sett_folder)
                except:
                    pass

            # network------------------------------------------------
            elif event == 'speed_limit_switch':
                switch = values['speed_limit_switch']

                if switch:
                    self.window['speed_limit'](disabled=False)
                else:
                    config.speed_limit = 0
                    self.window['speed_limit']('', disabled=True)  # clear and disable

            elif event == 'speed_limit':
                sl = values['speed_limit']

                # if no units entered will assume it KB
                try:
                    _ = int(sl)  # will succeed if it has no string
                    sl = f'{sl} KB'
                except:
                    pass

                sl = parse_bytes(sl)
                config.speed_limit = sl

            elif event == 'max_concurrent_downloads':
                config.max_concurrent_downloads = int(values['max_concurrent_downloads'])

            elif event == 'max_connections':
                mc = int(values['max_connections'])
                if mc > 0:
                    config.max_connections = mc

            elif event in ('raw_proxy', 'http', 'https', 'socks4', 'socks5', 'proxy_type', 'enable_proxy', 'use_proxy_dns'):
                self.set_proxy()

            elif event in ('use_referer', 'referer_url'):
                config.use_referer = values['use_referer']
                if config.use_referer:
                    self.window['referer_url'](disabled=False)
                    config.referer_url = self.window['referer_url'].get()
                else:
                    self.window['referer_url'](disabled=True)
                    config.referer_url = ''

            elif event in ('use_cookies', 'cookie_file_path'):
                config.use_cookies = values['use_cookies']
                if config.use_cookies:
                    self.window['cookie_file_path'](disabled=False)
                    config.cookie_file_path = self.window['cookie_file_path'].get()
                else:
                    self.window['cookie_file_path'](disabled=True)
                    config.cookie_file_path = ''

                # print(config.cookie_file_path)

            elif event in ('username', 'password', 'use_web_auth'):
                if values['use_web_auth']:
                    # enable widgets
                    self.window['username'](disabled=False)
                    self.window['password'](disabled=False)

                    config.username = values['username']
                    config.password = values['password']
                else:
                    config.username = ''
                    config.password = ''

                    # disable widgets
                    self.window['username'](disabled=True)
                    self.window['password'](disabled=True)

                # log('user, pass:', config.username, config.password)

            # update -------------------------------------------------
            elif event == 'update_frequency':
                selected = values['update_frequency']
                config.update_frequency = config.update_frequency_map[selected]  # selected
                # print('config.update_frequency:', config.update_frequency)

            elif event == 'update_youtube_dl':
                self.update_ytdl()

            elif event == 'rollback_ytdl_update':
                Thread(target=update.rollback_ytdl_update).start()
                self.select_tab('Log')

            elif event in ['update_pyIDM']:
                Thread(target=self.update_app, daemon=True).start()

            # systray -------------------------------------------------
            elif event in ('radio_close', 'radio_minimize', 'radio_quit'):
                config.close_action = event.replace('radio_', '')

            # Advanced -------------------------------------------------
            elif event == 'keep_temp':
                config.keep_temp = values['keep_temp']

            elif event == 'TEST_MODE':
                config.TEST_MODE = values['TEST_MODE']

            elif event == 'checksum':
                config.checksum = values['checksum']

            elif event == 'use_thread_pool_executor':
                config.use_thread_pool_executor = values['use_thread_pool_executor']

            # log ---------------------------------------------------------------------------------------------------
            elif event == 'log_level':
                config.log_level = int(values['log_level'])
                log('Log Level changed to:', config.log_level)

            elif event == 'clear_log':
                try:
                    self.window['log']('')
                except:
                    pass
            elif event in ['copy_log', 'open_log_file', 'open_log_folder']:
                file = os.path.join(config.sett_folder, 'log.txt')
                if event == 'open_log_file':
                    open_file(file)
                elif event == 'open_log_folder':
                    open_folder(file)
                elif event == 'copy_log':
                    with open(file) as f:
                        clipboard.copy(f.read())
                        log('Log text copied to clipboard', showpopup=True)

            # Run every n seconds -----------------------------------------------------------------------------------
            if time.time() - self.timer1 >= 1:
                self.timer1 = time.time()

                # gui update
                self.update_gui()

                # read incoming requests and messages from queue
                self.read_q()

                # scheduled downloads
                self.check_scheduled()

                # process pending jobs
                if self.pending and len(self.active_downloads) < config.max_concurrent_downloads:
                    self.start_download(self.pending.popleft(), silent=True)

            # run active windows
            for win in self.active_windows:
                win.run()
            self.active_windows = [win for win in self.active_windows if win.active]  # update active list

            # run one time, reason this is here not in setup, is to minimize gui loading time
            if self.one_time:
                self.one_time = False
                
                # check availability of ffmpeg in the system or in same folder with this script
                self.ffmpeg_check()

                # print last check for update
                if config.update_frequency < 0:
                    log('check for update is disabled!')

            # check for update block, negative values for config.last_update_check mean never check for update
            if config.update_frequency >= 0 and time.time() - self.check_for_update_timer >= 60:
                self.check_for_update_timer = time.time()

                t = time.localtime()
                today = t.tm_yday  # today number in the year range (1 to 366)

                if config.last_update_check == 0:  # no setting.cfg file found / fresh start
                    config.last_update_check = today
                else:
                    try:
                        if today < config.last_update_check:  # new year
                            days_since_last_update = today + 366 - config.last_update_check
                        else:
                            days_since_last_update = today - config.last_update_check

                        if days_since_last_update >= config.update_frequency:
                            log('days since last check for update:', days_since_last_update, 'day(s).')
                            log('asking user permission to check for update')
                            response = sg.PopupOKCancel('PyIDM reminder to check for updates!',
                                                        f'days since last check: {days_since_last_update} day(s).',
                                                        'you can change frequency or disable check for update from settings\n', title='Reminder')
                            if response == 'OK':
                                # it will check for updates and offer auto-update for frozen app. version
                                Thread(target=self.update_app, daemon=True).start()
                                # Thread(target=self.check_for_update, daemon=True).start()
                                config.last_update_check = today
                            else:
                                config.last_update_check = 0
                                log('check for update cancelled by user, next reminder will be after',
                                    config.update_frequency, 'day(s).')
                    except Exception as e:
                        log('MainWindow.run()>', e)

            # reset statusbar periodically
            if time.time() - self.statusbar_timer >= 10:
                self.statusbar_timer = time.time()
                self.set_status('')

        except Exception as e:
            log('Main window - Run()>', e)
            if config.TEST_MODE:
                raise e

    # region download
    @property
    def active_downloads(self):
        # update active downloads
        _active_downloads = set(d.id for d in self.d_list if d.status == config.Status.downloading)
        config.active_downloads = _active_downloads

        return _active_downloads

    def start_download(self, d, silent=False, force_window=False, downloader=None):
        """
         Receive a DownloadItem, do required checks then pass it to brain
        :param d:
        :param silent: if True, hide all a warnning dialogues and hide download window
        :param force_window: if True, download window will be shown, overriding silent option
        :param downloader: name of alternative  downloader, currently not implemented
        :return: 'cancelled', 'error', or None on success
        """

        if d is None or not d.url:
            return 'cancelled'

        # check unsupported protocols
        unsupported = ['f4m', 'ism']
        match = [item for item in unsupported if item in d.subtype_list]
        if match:
            log(f'unsupported protocol: \n"{match[0]}" stream type is not supported yet', start='', showpopup=True)
            return 'cancelled'

        # check for ffmpeg availability in case this is a dash video or hls video
        if 'dash' in d.subtype_list or 'hls' in d.subtype_list:
            # log('Dash video detected')
            if not self.ffmpeg_check():
                log('Download cancelled, FFMPEG is missing')
                return 'cancelled'

        # validate destination folder for existence and permissions
        # in case of missing download folder value will fallback to current download folder
        folder = d.folder or config.download_folder
        try:
            with open(os.path.join(folder, 'test'), 'w') as test_file:
                test_file.write('0')
            os.unlink(os.path.join(folder, 'test'))

            # update download item
            d.folder = folder
        except FileNotFoundError:
            sg.Popup(f'destination folder {folder} does not exist', title='folder error')
            return 'error'
        except PermissionError:
            sg.Popup(f"you don't have enough permission for destination folder {folder}", title='folder error')
            return 'error'
        except Exception as e:
            sg.Popup(f'problem in destination folder {repr(e)}', title='folder error')
            return 'error'

        # validate file name
        if d.name == '':
            sg.popup("File name can't be empty!!", title='invalid file name!!')
            return 'error'

        # check if file with the same name exist in destination
        if os.path.isfile(d.target_file):
            # auto rename option
            if config.auto_rename:
                d.name = auto_rename(d.name, d.folder)
                log('File with the same name exist in download folder, generate new name:', d.name)
            else:
                #  show dialogue
                msg = 'File with the same name already exist in ' + d.folder + '\n Do you want to overwrite file?'
                response = sg.PopupYesNo(msg)

                if response != 'Yes':
                    log('Download cancelled by user')
                    return 'cancelled'
                else:
                    delete_file(d.target_file)

        # ------------------------------------------------------------------
        # search current list for previous item with same name, folder
        found_index = self.file_in_d_list(d.target_file)
        if found_index is not None:  # might be zero, file already exist in d_list
            log('download item', d.num, 'already in list, check resume availability')
            # get download item from the list
            d_from_list = self.d_list[found_index]
            d.id = d_from_list.id

            # default
            response = 'Resume'

            if not silent:
                #  show dialogue
                msg = f'File with the same name: \n{self.d.name},\n already exist in download list\n' \
                      'Do you want to resume this file?\n' \
                      'Resume ==> continue if it has been partially downloaded ... \n' \
                      'Overwrite ==> delete old downloads and overwrite existing item... \n' \
                      'note: "if you need fresh download, you have to change file name \n' \
                      'or target folder or delete same entry from download list'
                window = sg.Window(title='', layout=[[sg.T(msg)], [sg.B('Resume'), sg.B('Overwrite', font='any 9 bold'), sg.B('Cancel')]])
                response, _ = window()
                window.close()

            #
            if response == 'Resume':
                log('check resuming?')

                # to resume, size must match, otherwise it will just overwrite
                if d.size == d_from_list.size and d.selected_quality == d_from_list.selected_quality:
                    log('resume is possible')
                    # get the same segment size
                    d.segment_size = d_from_list.segment_size
                    d.downloaded = d_from_list.downloaded
                else:
                    if not silent:
                        msg = f'Resume not possible, New "download item" has differnet properties than existing one \n' \
                              f'New item    : size={size_format(d.size)}, selected quality={d.selected_quality}\n' \
                              f'current item: size={size_format(d_from_list.size)}, selected quality={d_from_list.selected_quality}\n' \
                              f'if you continue, previous download will be overwritten'
                        event = sg.PopupOKCancel(msg)
                        if event != 'OK':
                            log('aborted by user')
                            return 'cancelled'
                    log('file:', d.name, 'has different properties and will be downloaded from beginning')
                    d.delete_tempfiles(force_delete=True)

                # replace old item in download list
                self.d_list[found_index] = d

            elif response == 'Overwrite':
                log('overwrite')
                d.delete_tempfiles(force_delete=True)

                # replace old item in download list
                self.d_list[found_index] = d

            else:
                log('Download cancelled by user')
                d.status = Status.cancelled
                return 'cancelled'

        # ------------------------------------------------------------------

        else:  # new file
            log('fresh file download')
            # generate unique id number for each download
            d.id = len(self.d_list)

            # add to download list
            self.d_list.append(d)

        # if max concurrent downloads exceeded, this download job will be added to pending queue
        if len(self.active_downloads) >= config.max_concurrent_downloads:
            d.status = Status.pending
            self.pending.append(d)
            return None

        # create download window and append to active list
        if config.show_download_window and (not silent or force_window):
            self.show_download_window(d)

        # create and start brain in a separate thread
        Thread(target=brain, daemon=True, args=(d, downloader)).start()

        # select row in downloads table
        self.update_table()
        self.select_row(d.id)

        return None

    def stop_all_downloads(self):
        # change status of pending items to cancelled
        for d in self.d_list:
            d.status = Status.cancelled

        self.pending.clear()

    def resume_all_downloads(self):
        response = sg.PopupOKCancel('Resume "ALL" items?',
                                    'Note: to resume single item use "Resume" button at top left of "Downloads tab"',
                                    'Are you sure?')
        if response != 'OK':
            return

        # change status of all non completed items to pending
        for d in self.d_list:
            if d.status == Status.cancelled:
                self.start_download(d, silent=True)

    def file_in_d_list(self, target_file):
        for i, d in enumerate(self.d_list):
            if d.target_file == target_file:
                return i
        return None

    def download_btn(self, downloader=None):

        if not self.d:
            sg.popup_ok('Nothing to download')
            return
        elif not self.url:
            sg.popup_ok('Nothing to download, you should add url first')
            return
        elif not self.d.type:
            response = sg.PopupOKCancel('None type or bad response code', 'Force download?')
            if response != 'OK':
                return
        elif self.d.type == 'text/html':
            response = sg.popup_ok_cancel('Contents might be a web page / html, Download anyway?')
            if response != 'OK':
                return
            else:
                self.d.accept_html = True

        # make sure video streams loaded successfully before start downloading
        if self.video and not self.video.all_streams:
            if 0 < self.m_bar < 100:
                msg = 'Video still loading streams, \nplease wait until loading and select a proper video quality'
            else:
                msg = 'Video does not have any streams, and can not be downloaded'

            sg.PopupOK(msg)
            return

        # check if name has a proper extension
        name, ext = os.path.splitext(self.d.name)
        if ext != self.d.extension:
            response = sg.popup_ok_cancel('File name does not have a correct extension', 'New name will be:',
                                          name + self.d.extension)
            if response != 'OK':
                return
            self.d.name = validate_file_name(name + self.d.extension)

        # get copy of current download item
        d = copy.copy(self.d)
        d.folder = config.download_folder

        # dash audio
        if 'dash' in d.subtype_list and config.manually_select_dash_audio:
            # manually select dash audio
            self.select_dash_audio(d)

        r = self.start_download(d, downloader=downloader)

        if r not in ('error', 'cancelled', False):
            self.select_tab('Downloads')

    # endregion

    # region downloads tab
    @property
    def selected_d(self):
        self._selected_d = self.d_list[self.selected_row_num] if self.selected_row_num is not None else None
        return self._selected_d

    @selected_d.setter
    def selected_d(self, value):
        self._selected_d = value

    @staticmethod
    def format_cell_data(k, v):
        """take key, value and prepare it for display in cell"""
        if k in ['size', 'total_size', 'downloaded']:
            v = size_format(v)
        elif k == 'speed':
            v = size_format(v, '/s')
        elif k in ('percent', 'progress'):
            v = f'{v}%' if v else '---'
        elif k == 'time_left':
            v = time_format(v)
        elif k == 'resumable':
            v = 'yes' if v else 'no'
        elif k == 'name':
            v = validate_file_name(v)

        return v

    def resume_btn(self):
        if not self.selected_d or self.selected_d.status in (Status.downloading, Status.processing):
            return

        self.start_download(self.selected_d, silent=True, force_window=True)

    def cancel_btn(self):
        if self.selected_row_num is None:
            return

        d = self.selected_d
        if d.status == Status.completed:
            return

        d.status = Status.cancelled

        if d.status == Status.pending:
            self.pending.pop(d.id)

    def delete_btn(self):
        if self.selected_row_num is None:
            return

        # todo: should be able to delete items anytime by making download item id unique and number changeable
        # abort if there is items in progress or paused
        if self.selected_d.status in (Status.downloading, Status.processing):
            msg = "Can't delete items while downloading.\nStop or cancel downloading first!"
            sg.Popup(msg)
            return

        # confirm to delete
        msg = f"Warning!!!\nAre you sure you want to delete!\n{self.selected_d.name}\n"
        r = sg.PopupYesNo(msg, title='Delete file?', keep_on_top=True)
        if r != 'Yes': return

        try:
            # pop item
            d = self.d_list.pop(self.selected_row_num)

            # update count numbers for remaining items
            n = len(self.d_list)
            for i in range(n):
                self.d_list[i].id = i

            # fix a selected item number if it no longer exist
            if not self.d_list:
                self.selected_row_num = None
            else:
                last_num = len(self.d_list) - 1
                if self.selected_row_num > last_num:
                    self.selected_row_num = last_num

            # delete temp folder on disk
            d.delete_tempfiles(force_delete=True)

        except:
            pass

    def delete_all_downloads(self):
        """delete all items in downloads table including their temp files, active items 
        which has "downloading" or "processing status" will be execluded"""

        # warning / confirmation dialog, user has to write ok to proceed
        msg = 'Delete all -"non active"- items and their progress temp files\n' \
              'Type the word "delete" or letter "d" and hit ok\n'
        response = sg.PopupGetText(msg, title='Warning!!', keep_on_top=True)
        if response in ('delete', 'd'):
            log('start deleting all download items')
        else:
            return

        # selected item number
        self.selected_row_num = None

        # delete temp files
        for d in self.d_list:
            if d.status not in (Status.downloading, Status.processing):
                Thread(target=d.delete_tempfiles, args=[True], daemon=True).start()
        
        # update d_list
        self.d_list = config.d_list = [d for d in self.d_list if d.status in (Status.downloading, Status.processing)]
        
        if self.d_list:
            # correct id's
            for i, d in enumerate(self.d_list):
                d.id = i

            # selected item number
            self.selected_row_num = self.d_list[-1].id


    def refresh_link_btn(self):
        if self.selected_row_num is None:
            return

        d = self.selected_d
        self.requested_quality = d.selected_quality
        config.download_folder = d.folder

        self.url = ''
        self.window['url'](d.url)
        self.on_url_text_change()

        self.window['folder'](config.download_folder)
        self.select_tab('Main')

    # endregion

    # region video

    @property
    def m_bar(self):
        """playlist progress bar"""
        return self._m_bar

    @m_bar.setter
    def m_bar(self, value):
        """playlist progress bar"""
        self._m_bar = value if value <= 100 else 100
        try:
            self.window['m_bar'].update_bar(value)
        except Exception as e:
            print('update main bar failed', e)

    @property
    def s_bar(self):
        """playlist progress bar"""
        return self._s_bar

    @s_bar.setter
    def s_bar(self, value):
        """playlist progress bar"""
        self._s_bar = value if value <= 100 else 100
        try:
            self.window['s_bar'].update_bar(value)
        except:
            pass

    @property
    def pl_menu(self):
        """video playlist menu"""
        return self._pl_menu

    @pl_menu.setter
    def pl_menu(self, rows):
        """video playlist menu"""
        self._pl_menu = rows

        try:
            # fit text into widget  FIXME:(killing and time consuming code)
            # rows = [self.fit_text(text, self.window['pl_menu'].Size[0]) for text in rows]

            # update widget
            self.window['pl_menu'](values=rows)
        except:
            pass

    @property
    def stream_menu(self):
        """video streams menu"""
        return self._stream_menu

    @stream_menu.setter
    def stream_menu(self, rows):
        """video streams menu"""
        self._stream_menu = rows
        try:
            # fit text into widget
            rows = [self.fit_text(text, self.window['stream_menu'].Size[0]) for text in rows]
            self.window['stream_menu'](values=rows)
        except:
            pass

    def reset_video_controls(self):
        try:
            self.reset_progress_bar()

            self.pl_menu = ['Playlist']
            self.window['pl_menu'](value='Playlist')

            self.stream_menu = ['Video quality']
            self.window['stream_menu'](value='Video quality')

            self.window['playlist_frame'](value='Playlist/video:')
            self.window['format_id']('')

            # reset thumbnail
            self.reset_thumbnail()

            # reset tooltips
            self.set_tooltip(widget=self.window['pl_menu'], tooltip_text='')
            self.set_tooltip(widget=self.window['stream_menu'], tooltip_text='')
        except:
            pass

    def reset_progress_bar(self):
        self.m_bar = 0
        self.s_bar = 0

    def reset_thumbnail(self):
        """show a blank thumbnail background"""
        self.show_thumbnail(thumbnail=None)

    def show_thumbnail(self, thumbnail=None):
        """show video thumbnail in thumbnail image widget in main tab, call without parameter reset thumbnail"""

        try:
            if thumbnail is None:
                # reset thumbnail
                self.window['main_thumbnail'](data=thumbnail_icon)

            elif config.show_thumbnail and thumbnail != self.current_thumbnail:
                # new thumbnail
                self.window['main_thumbnail'](data=thumbnail)

            self.current_thumbnail = thumbnail
        except Exception as e:
            log('show_thumbnail()>', e)

    def youtube_func(self):
        """fetch metadata from youtube and other stream websites, it is intended to be run as a separate thread,
        it is recommended to use "execute command" function if need to run any gui related method, tkinter will complain
        "not running from main thread" error
        """

        if not self.url:
            return

        def cancel_flag():
            # quit if main window terminated
            if config.terminate:
                return True

            # quit if url changed by user
            if url != self.url:
                self.reset_video_controls()
                self.reset()
                log('youtube func: quitting, url changed by user')
                return True

            # quit if new youtube func thread started
            if yt_id != self.yt_id:
                log('youtube func: quitting, new instance has started')
                return True

            else:
                return False

        # getting videos from youtube is time consuming, if another thread starts, it should cancel the previous one
        # create unique identification for this thread
        self.yt_id += 1 if self.yt_id < 1000 else 0
        yt_id = self.yt_id
        url = self.d.url

        msg = f'looking for video streams ... Please wait'
        log(msg)
        log('youtube_func()> processing:', self.d.url)

        # reset video controls
        self.reset_video_controls()

        # reset abort flag
        config.ytdl_abort = False

        # main progress bar initial indication
        self.m_bar = 10
        self.set_cursor('busy')

        # reset playlist
        self.playlist = []

        # check cancel flag
        if cancel_flag(): return

        try:
            # we import youtube-dl in separate thread to minimize startup time, will wait in loop until it gets imported
            if video.ytdl is None:
                log('youtube-dl module still not loaded completely, please wait')
                while not video.ytdl:
                    time.sleep(1)  # wait until module gets imported

            # youtube-dl process
            timer1 = time.time()
            log(get_ytdl_options())
            with video.ytdl.YoutubeDL(get_ytdl_options()) as ydl:
                # process=False is faster and youtube-dl will not download every videos webpage in the playlist
                info = ydl.extract_info(self.d.url, download=False, process=False)

                # don't process direct links, youtube-dl warning message "URL could be a direct video link, returning it as such."
                # refer to youtube-dl/extractor/generic.py
                if not info or info.get('direct'):
                    log('youtube_func()> No streams found')
                    self.reset_video_controls()
                    return

                # set playlist / video title
                self.pl_title = info.get('title', '')

                # 25% done
                self.m_bar = 25

                """
                _type key:
                
                _type "playlist" indicates multiple videos.
                    There must be a key "entries", which is a list, an iterable, or a PagedList
                    object, each element of which is a valid dictionary by this specification.
                    Additionally, playlists can have "id", "title", "description", "uploader",
                    "uploader_id", "uploader_url" attributes with the same semantics as videos
                    (see above).
                
                _type "multi_video" indicates that there are multiple videos that
                    form a single show, for examples multiple acts of an opera or TV episode.
                    It must have an entries key like a playlist and contain all the keys
                    required for a video at the same time.
                
                _type "url" indicates that the video must be extracted from another
                    location, possibly by a different extractor. Its only required key is:
                    "url" - the next URL to extract.
                    The key "ie_key" can be set to the class name (minus the trailing "IE",
                    e.g. "Youtube") if the extractor class is known in advance.
                    Additionally, the dictionary may have any properties of the resolved entity
                    known in advance, for example "title" if the title of the referred video is
                    known ahead of time.
                
                _type "url_transparent" entities have the same specification as "url", but
                    indicate that the given additional information is more precise than the one
                    associated with the resolved URL.
                    This is useful when a site employs a video service that hosts the video and
                    its technical metadata, but that video service does not embed a useful
                    title, description etc.
                """
                _type = info.get('_type', 'video')

                # handle types: url and url transparent
                if _type in ('url', 'url_transparent'):
                    # handle youtube user links ex: https://www.youtube.com/c/MOTORIZADO/videos
                    # issue: https://github.com/pyIDM/PyIDM/issues/146
                    # info: {'_type': 'url', 'url': 'https://www.youtube.com/playlist?list=UUK32F9z7s_JhACkUdVoWdag',
                    # 'ie_key': 'YoutubePlaylist', 'extractor': 'youtube:user', 'webpage_url': 'https://www.youtube.com/c/MOTORIZADO/videos',
                    # 'webpage_url_basename': 'videos', 'extractor_key': 'YoutubeUser'}

                    info = ydl.extract_info(info['url'], download=False, ie_key=info.get('ie_key'), process=False)

                # 50% done
                self.m_bar = 50

                # check results if _type is a playlist / multi_video -------------------------------------------------
                if _type in ('playlist', 'multi_video') or 'entries' in info:
                    log('youtube-func()> start processing playlist')
                    log('Media info:', info, log_level=3)

                    # videos info
                    pl_info = list(info.get('entries'))

                    playlist_url = self.d.url

                    # create initial playlist with un-processed video objects
                    for num, v_info in enumerate(pl_info):
                        v_info['formats'] = []

                        # get video's url
                        vid_url = v_info.get('webpage_url', None) or v_info.get('url', None) or v_info.get('id', None)

                        # create video object
                        vid = Video(vid_url, v_info)

                        # update info
                        vid.playlist_title = info.get('title', '')
                        vid.playlist_url = playlist_url

                        # add video to playlist
                        self.playlist.append(vid)

                    # increment to media progressbar to complete last 50%
                    playlist_length = len(self.playlist)
                    m_bar_incr = 50 / playlist_length

                    # update playlist title widget: show how many videos
                    try:
                        self.window['playlist_frame'](
                            value=f'Playlist ({playlist_length} {"videos" if playlist_length > 1 else "video"}):')
                    except:
                        pass

                    # update playlist menu, only videos names, there is no videos qualities yet
                    # self.update_pl_menu()
                    execute_command('update_pl_menu')

                    # user notification for big playlist
                    if config.process_playlist and playlist_length > config.big_playlist_length:
                        popup(f'Big playlist detected with {playlist_length} videos \n'
                              f'To avoid wasting time and resources, videos info will be processed only when \n'
                              f'selected in main Tab or playlist window\n',
                              title='big playlist detected')

                    # process videos info
                    num = 0
                    v_threads = []
                    processed_videos = 0

                    if self.playlist:
                        # create threads to get videos info
                        while True:
                            if not config.process_playlist or playlist_length > config.big_playlist_length:
                                break

                            time.sleep(0.01)

                            # create threads
                            if processed_videos < playlist_length:
                                t = Thread(target=process_video_info, daemon=True, args=(self.playlist[num],))
                                v_threads.append(t)
                                t.start()
                                # print('processed video:', num + 1)
                                processed_videos += 1

                            # check for finished threads
                            for t in v_threads[:]:
                                if not t.is_alive():
                                    v_threads.pop()
                                    self.m_bar += m_bar_incr

                            # check cancel flag
                            if cancel_flag():
                                return

                            # check if done
                            if processed_videos == playlist_length and not v_threads:
                                break

                            if num < playlist_length - 1:
                                num += 1

                else:
                    # one video, not a playlist, processing info
                    # process_video_info() will fail to get formats for some videos
                    # https://vod.tvp.pl/video/rozmowy-przy-wycinaniu-lasu,rozmowy-przy-wycinaniu-lasu,21765408

                    # process info
                    info = ydl.process_ie_result(info, download=False)

                    if info:
                        # check for formats, and inform user
                        if not info.get('formats'):
                            log('youtube func: missing formats, re-downloading webpage')

                            # to avoid missing formats will call youtube-dl again with process=True 're-downloading webpage'
                            info = ydl.extract_info(self.d.url, download=False, process=True)

                        log('processed media info:', info, log_level=3)

                    else:  # if returned None
                        log('youtube_func()> No streams found')
                        self.reset_video_controls()
                        return

                    vid = Video(self.d.url, vid_info=info)

                    # report done processing
                    vid.processed = True

                    # add to playlist
                    self.playlist = [vid]

                    # update playlist menu
                    execute_command('update_pl_menu')
                    # self.update_pl_menu()

                    # get thumbnail
                    Thread(target=vid.get_thumbnail, daemon=True).start()

            # quit if we couldn't extract any videos info (playlist or single video)
            if not self.playlist:
                self.reset()
                log('youtube func: quitting, can not extract videos')
                return

            # check cancel flag
            if cancel_flag(): return

            # job completed
            self.m_bar = 100
            log(f'youtube_func()> done fetching information in {round(time.time() - timer1, 1)} seconds .............')

        except Exception as e:
            log('youtube_func()> error:', e)
            if config.TEST_MODE:
                raise e
            self.reset_video_controls()

        finally:
            self.set_cursor('default')

    def update_pl_menu(self):
        try:
            # set playlist label
            num = len(self.playlist)

            # set video frame title ex: "Playlist (20) videos:"
            self.window['playlist_frame'](value=f'Playlist ({num} {"videos" if num > 1 else "video"}):')

            # update playlist menu items
            log('preparing playlist menu...')
            self.pl_menu = [str(i + 1) + '- ' + video.rendered_name for i, video in enumerate(self.playlist)]

            # choose first item in playlist
            self.window['pl_menu'].Widget.current(0)
            self.playlist_on_choice()

        except Exception as e:
            log('update_pl_menu()> error', e)

    def update_stream_menu(self):
        try:
            self.stream_menu = self.video.stream_menu

            # check if there any requested quality / stream
            if self.requested_quality and self.requested_quality in self.stream_menu:
                index = self.stream_menu.index(self.requested_quality)

                # reset requested quality, because it's one time use only
                self.requested_quality = None
            else:
                index = 1

            self.window['stream_menu'].Widget.current(index)  # tkinter set current selected index

            self.stream_on_choice()

        except:
            pass

    def playlist_on_choice(self):

        try:
            # index = self.pl_menu.index(selected_text)
            selected_index = self.window['pl_menu'].Widget.current()  # tkinter return current selected index
            self.video = self.playlist[selected_index]

            # set current download item as self.video
            # self.video.url = self.d.url
            self.d = self.video

            self.update_stream_menu()

            # instant widgets update
            self.update_gui()

            # fetch video info if not available and animate side bar
            if self.video and not self.video.processed:
                self.s_bar = 0

                # process video
                Thread(target=process_video_info, daemon=True, args=(self.video, )).start()
            else:
                self.s_bar = 100

            # set tooltip
            self.set_tooltip(widget=self.window['pl_menu'], tooltip_text=self.pl_menu[selected_index])

        except Exception as e:
            log('playlist_OnChoice()> error', e)

    def stream_on_choice(self):

        try:
            # Find and update video selected stream, use index not selected text, to avoid selecting wrong stream
            #  in case of similar / repeated names in stream menu
            selected_index = self.window['stream_menu'].Widget.current()  # tkinter return current selected index

            # update video's selected stream
            self.video.select_stream(index=selected_index, update=True)

            # display selected stream name including format Id
            self.window['format_id'](self.video.selected_stream.name.strip())

            # update gui
            self.update_gui()

            # set dynamic tooltip -----------------------------------------------------------------------------------
            self.set_tooltip(widget=self.window['stream_menu'], tooltip_text=self.stream_menu[selected_index])

        except Exception as e:
            log('stream_OnChoice', e, log_level=3)
            if config.TEST_MODE:
                raise e

    def set_tooltip(self, widget=None, tooltip_text=None):
        """
        change and Show tooltip without moving mouse pointer, correct tooltip glitches in case of dynamic changes
        :param widget: PySimpleGUI Element object, example window['mywidget']
        :param tooltip_text: text
        :return: None
        """

        try:
            # disable tooltip if no text
            if not tooltip_text:
                if widget.TooltipObject:
                    # widget still bounded to ToolTip.enter/leave , we can unbind it or simply mask schedule function
                    # for more details refer to PySimpleGUI "ToolTip" class
                    # self.widget.bind("<Enter>", self.enter)
                    # self.widget.bind("<Leave>", self.leave)
                    widget.TooltipObject.schedule = lambda: None
                return

            # add leading and trailing space for tooltip to look more natural
            tooltip_text = f' {tooltip_text} '

            # first hide any existing tooltip if any.
            if widget.TooltipObject:
                # call leave() "unschedule and hide" will cancel any sched. tooltip and hide current
                widget.TooltipObject.leave()

            # set new tooltip
            widget.set_tooltip(tooltip_text)

            # get current mouse x, y
            root = self.window.TKroot
            x, y = root.winfo_pointerxy()

            # find current widget under mouse
            widget_under_mouse = root.winfo_containing(x, y)

            # if our widget under mouse will show tooltip
            if widget.Widget == widget_under_mouse:
                # x,y position of our widget
                w_x, w_y = widget_under_mouse.winfo_rootx(), widget_under_mouse.winfo_rooty()

                # assign relative x, y to tooltip object
                widget.TooltipObject.x = x - w_x
                widget.TooltipObject.y = y - w_y

                # show tooltip now
                widget.TooltipObject.showtip()

        except Exception as e:
            log('set_tooltip()', e, log_level=3)

    def download_playlist(self):
        # check if playlist is ready
        if not self.playlist:
            sg.popup_ok('Playlist is empty, nothing to download :(', title='Playlist download')
            return

        # check if "subtitle_window" is already opened
        found = [window for window in self.active_windows if isinstance(window, PlaylistWindow)]
        if found:
            window = found[0]

            # bring window to front
            window.focus()

        else:  # not found
            window = PlaylistWindow(self.playlist)
            self.active_windows.append(window)

    def download_subtitles(self):
        # for hls videos: if there is no subtitle, will try to find one
        if not self.d.subtitles and 'hls' in self.d.subtype_list:
            log('trying to get a subtitle')

            # download master m3u8 file
            m3u8_doc = download_m3u8(self.d.manifest_url, http_headers=self.d.http_headers)

            if m3u8_doc:
                log('parsing subs')
                self.d.subtitles = parse_subtitles(m3u8_doc, self.d.manifest_url)

        if not (self.d.subtitles or self.d.automatic_captions):
            sg.PopupOK("No Sub's available")
            return

        # check if "subtitle_window" is already opened
        found = [window for window in self.active_windows if isinstance(window, SubtitleWindow)]
        if found:
            window = found[0]

            # bring window to front
            window.focus()

        else:  # not found
            # create new window and append it to active windows
            window = SubtitleWindow(self.d)
            self.active_windows.append(window)

    def ffmpeg_check(self):
        if not check_ffmpeg():
            if config.operating_system == 'Windows':
                layout = [[sg.T('"ffmpeg" is missing!! and need to be downloaded:\n')],
                          [sg.T('destination:')],
                          [sg.Radio(f'recommended: {config.global_sett_folder}', group_id=0, key='radio1', default=True)],
                          [sg.Radio(f'Local folder: {config.current_directory}', group_id=0, key='radio2')],
                          [sg.B('Download'), sg.Cancel()]]

                window = sg.Window('ffmpeg is missing', layout)

                event, values = window()
                window.close()
                selected_folder = config.global_sett_folder if values['radio1'] else config.current_directory
                if event == 'Download':
                    download_ffmpeg(destination=selected_folder)
            else:
                sg.popup_error(
                    '"ffmpeg" is required to merge an audio stream with your video',
                    'executable must be copied into PyIDM folder or add ffmpeg path to system PATH',
                    '',
                    'you can download it manually from https://www.ffmpeg.org/download.html',
                    title='ffmpeg is missing')

            return False
        else:
            return True

    def select_dash_audio(self, d):
        """prompt user to select dash audio manually"""
        if 'dash' not in d.subtype_list:
            log('select_dash_audio()> this function is available only for a dash video, ....')
            return

        audio_streams = [stream for stream in d.all_streams if stream.mediatype == 'audio']
        if not audio_streams:
            log('select_dash_audio()> there is no audio streams available, ....')
            return

        streams_menu = [stream.name for stream in audio_streams]
        layout = [
            [sg.T('Select audio stream to be merged with dash video:')],
            [sg.Combo(streams_menu, default_value=d.audio_stream.name, key='stream')],
            [sg.T('please note:\n'
                  'Selecting different audio format than video format, may takes longer time while merging')],
            [sg.T('')],
            [sg.Ok(), sg.Cancel()]
        ]

        window = sg.Window('Select dash audio', layout, finalize=True)

        # while True:
        event, values = window()

        if event == 'Ok':
            selected_stream_index = window['stream'].Widget.current()
            selected_stream = audio_streams[selected_stream_index]

            # set audio stream
            d.select_audio(audio_stream=selected_stream)
            # print(self.d.audio_stream.name)
        window.close()

        # print(event, values)
    # endregion

    # region General
    def on_url_text_change(self):
        url = self.window['url'].get().strip()

        if url == self.url:
            return

        self.url = url

        # Focus and select main app page in case text changed from script
        self.window.BringToFront()
        self.select_tab('Main')

        # reset parameters and create new download item
        self.reset()
        try:
            self.set_cursor('busy')
            self.d.eff_url = self.d.url = url

            self.d.folder = config.download_folder

            Thread(target=self.fetch_info, args=[url], daemon=True).start()

        except Exception as e:
            log('url_text_change()> error', e)
            if config.TEST_MODE:
                raise e
        finally:
            self.set_cursor('default')

    def fetch_info(self, url):
        self.d.update(url)

        # searching for videos
        # use size to identify a direct download links
        # can't depend on mime-type sent by server since it is not reliable
        # this HLS link will send mime-type as "application" https://dash.akamaized.net/dash264/TestCasesIOP33/multiplePeriods/2/manifest_multiple_Periods_Add_Remove_AdaptationSet.mpd
        if self.d.type == 'text/html' or self.d.size < 1024 * 1024:  # 1 MB as a max size
            self.youtube_func()

    def retry(self):
        self.url = ''
        self.on_url_text_change()

    def reset(self):
        # create new download item, the old one will be garbage collected by python interpreter
        self.d = DownloadItem()

        # reset some values
        self.set_status('')  # status bar
        self.playlist = []
        self.video = None

        # widgets
        self.reset_video_controls()
        self.window['status_code']('')

        # abort youtube-dl current operation
        config.ytdl_abort = True

        # Force python garbage collector to free up memory
        gc.collect()

    def set_cursor(self, cursor='default'):
        # can't be called before window.Read()
        if cursor == 'busy':
            cursor_name = 'watch'
        else:  # default
            cursor_name = 'arrow'

        try:
            self.window.TKroot['cursor'] = cursor_name
        except:
            pass

    def close_callback(self):
        """This callback override main window close"""
        # currently "systray" support windows only
        if config.close_action == 'minimize' and config.systray_active:
            self.hide()

            # notify
            notify('PyIDM still running in background', timeout=2)

        else:
            # closing window and terminate downloads
            self.active = False
            config.terminate = True

            if config.close_action == 'quit':
                config.shutdown = True

            root = self.window.TKroot

            root.quit()
            root.destroy()
            # self.window.RootNeedsDestroying = True
            # self.window.TKrootDestroyed = True

    def un_hide_active_windows(self):
        try:
            # un hide all active windows
            for obj in self.active_windows:
                obj.window.un_hide()
        except:
            pass

    def hide_active_windows(self):
        try:
            # hide all active windows
            for obj in self.active_windows:
                obj.window.hide()
        except:
            pass

    def hide(self):
        """close main window and hide other windows"""

        # hide active windows
        self.hide_active_windows()

        # hide main window
        self.window.hide()

    def un_hide(self):
        # show main window
        self.window.un_hide()

        # un hide active windows
        self.un_hide_active_windows()

    def close(self):
        """close window and stop updating"""
        self.active = False
        config.terminate = True

        # close active windows
        try:
            for obj in self.active_windows:
                obj.close()
        except:
            pass

        config.window_size = self.window.Size
        # log('main window closing')
        self.window.close()

        # Save setting to disk
        setting.save_setting()
        setting.save_d_list(config.d_list)

        # Force python garbage collector to free up memory
        gc.collect()

    def check_scheduled(self):
        t = time.localtime()
        c_t = (t.tm_hour, t.tm_min)
        for d in self.d_list:
            if d.sched and d.sched[0] <= c_t[0] and d.sched[1] <= c_t[1]:
                self.start_download(d, silent=True)  # send for download
                d.sched = None  # cancel schedule time

    def ask_for_sched_time(self, msg=''):
        """Show a gui dialog to ask user for schedule time for download items, it take one or more of download items"""
        response = None

        layout = [
            [sg.T('schedule download item:')],
            [sg.T(msg)],
            [sg.Combo(values=list(range(1, 13)), default_value=1, size=(5, 1), key='hours'), sg.T('H  '),
             sg.Combo(values=list(range(0, 60)), default_value=0, size=(5, 1), key='minutes'), sg.T('m  '),
             sg.Combo(values=['AM', 'PM'], default_value='AM', size=(5, 1), key='am pm')],
            [sg.Ok(), sg.Cancel()]
        ]

        window = sg.Window('Scheduling download item', layout, finalize=True)

        e, v = window()

        if e == 'Ok':
            h = int(v['hours'])
            if v['am pm'] == 'AM' and h == 12:
                h = 0
            elif v['am pm'] == 'PM' and h != 12:
                h += 12

            m = int(v['minutes'])

            # # assign to download item
            # d.sched = (h, m)

            response = h, m

        window.close()
        return response

    def set_proxy(self):
        enable_proxy = self.window['enable_proxy'].get()
        config.enable_proxy = enable_proxy

        config.use_proxy_dns = self.window['use_proxy_dns'].get()

        # enable disable proxy entry text
        self.window['raw_proxy'](disabled=not enable_proxy)

        if not enable_proxy:
            config.proxy = ''
            self.window['current_proxy_value']('_no proxy_')
            return

        # set raw proxy
        raw_proxy = self.window['raw_proxy'].get()
        config.raw_proxy = raw_proxy

        # proxy type
        proxy_type = self.window['proxy_type'].get()

        config.proxy_type = proxy_type

        # proxy dns
        if config.use_proxy_dns:
            if proxy_type == 'socks4':
                proxy_type = 'socks4a'
            elif proxy_type == 'socks5':
                proxy_type = 'socks5h'

        if raw_proxy:
            raw_proxy = raw_proxy.split('://')[-1]
            proxy = proxy_type + '://' + raw_proxy
        else:
            proxy = ''

        config.proxy = proxy
        self.window['current_proxy_value'](config.proxy)
        # print('config.proxy = ', config.proxy)

    # endregion

    # region update
    def update_app(self):
        """
        check for new version or update patch and show update window,
        this method is time consuming and should run from a thread
        """

        # check for new App. version
        changelog = update.check_for_new_version()
        if changelog:
            self.active_windows.append(UpdateWindow(changelog))

        else:
            # check for update patch -- for frozen versions only --
            batch_info = update.check_for_new_patch() if config.FROZEN else None

            if batch_info:
                self.active_windows.append(UpdateWindow(batch_info.get('description', 'No description available')))
            else:
                log('No Update available', showpopup=True, start='')

    def check_for_ytdl_update(self):
        config.ytdl_LATEST_VERSION = update.check_for_ytdl_update()
        log('youtube-dl, latest version = ', config.ytdl_LATEST_VERSION, ' - current version = ', config.ytdl_VERSION)

    def update_ytdl(self):
        current_version = config.ytdl_VERSION
        latest_version = config.ytdl_LATEST_VERSION or update.check_for_ytdl_update()
        if latest_version:
            config.ytdl_LATEST_VERSION = latest_version
            log('youtube-dl, latest version = ', latest_version, ' - current version = ', current_version)

            if latest_version != current_version:
                # select log tab
                self.select_tab('Log')

                response = sg.popup_ok_cancel(
                    f'Found new version of youtube-dl on github \n'
                    f'new version     =  {latest_version}\n'
                    f'current version =  {current_version} \n'
                    'Install new version?',
                    title='youtube-dl module update')

                if response == 'OK':
                    try:
                        Thread(target=update.update_youtube_dl).start()
                    except Exception as e:
                        log('failed to update youtube-dl module:', e)
            else:
                sg.popup_ok(f'youtube_dl is up-to-date, current version = {current_version}')
    # endregion


# Note every window class must have self.active property and close method
class DownloadWindow:

    def __init__(self, d=None):
        self.d = d
        self.window = None
        self.active = True
        self.timeout = 10
        self.timer = 0
        self._progress_mode = 'determinate'

        self.create_window()

    @property
    def progress_mode(self):
        return self._progress_mode

    @progress_mode.setter
    def progress_mode(self, mode):
        """change progressbar mode (determinate / undeterminate)"""
        if self._progress_mode != mode:
            try:
                self.window['progress_bar'].Widget.config(mode=mode)
                self._progress_mode = mode
            except:
                pass

    def create_window(self):
        layout = [
            [sg.T('', size=(60, 4), key='out')],

            [sg.T(' ' * 120, key='percent')],

            [sg.ProgressBar(max_value=100, key='progress_bar', size=(42, 15), border_width=3)],

            [sg.T(' ', key='status', size=(35, 1)), sg.Button('Hide', key='hide'), sg.Button('Cancel', key='cancel')],
            [sg.T(' ', font='any 1')],
            [sg.T('', size=(100, 1),  font='any 8', key='log2', relief=sg.RELIEF_RAISED)],
        ]

        self.window = sg.Window(title=self.d.name, layout=layout, finalize=True, margins=(2, 2), size=(460, 205), return_keyboard_events=True)
        self.window['progress_bar'].expand()
        self.window['percent'].expand()
        self.window['out'].expand()

        # log text, disable word wrap
        # self.window['log2'].Widget.config(wrap='none')

    def update_gui(self):
        # trim name and folder length
        name = truncate(self.d.rendered_name, 50)

        # folder = truncate(self.d.folder, 50)
        errors = f' ..connection errors!.. {self.d.errors}' if self.d.errors and self.d.status == Status.downloading else ''

        out = f"File: {name}\n" \
              f"downloaded: {size_format(self.d.downloaded)} out of {size_format(self.d.total_size)}\n" \
              f"speed: {size_format(self.d.speed, '/s') }  {time_format(self.d.time_left)} left \n" \
              f"live connections: {self.d.live_connections} - remaining parts: {self.d.remaining_parts} {errors}\n"

        try:
            self.window['out'](value=out)

            # progress bar mode depend on available downloaditem progress property
            if self.d.progress or self.d.status != Status.downloading:
                self.progress_mode = 'determinate'
                self.window['progress_bar'].update_bar(self.d.progress)
            else:  # size is zero, will make random animation
                self.progress_mode = 'indeterminate'
                self.window['progress_bar'].Widget['value'] += 5

            if self.d.status in (Status.completed, Status.cancelled, Status.error) and config.auto_close_download_window:
                self.close()

            # change cancel button to done when completed
            if self.d.status == Status.completed:
                self.window['cancel'](text='Done')

            # log
            self.window['log2'](config.log_entry)

            # percentage value to move with progress bar
            position = int(self.d.progress)
            self.window['percent'](f"{' ' * position} {self.d.progress}%")

            # status update
            self.window['status'](f"{self.d.status}  {self.d.i}")
        except:
            pass

    def run(self):
        self.event, self.values = self.window.Read(timeout=self.timeout)
        if self.event in ('cancel', None) or self.event.startswith('Escape'):  # escape button
            log('download window received', self.event)
            if self.d.status not in (Status.error, Status.completed):
                self.d.status = Status.cancelled
            self.close()

        elif self.event == 'hide':
            self.close()

        # update gui
        if time.time() - self.timer >= 0.5:
            self.timer = time.time()
            self.update_gui()

    def focus(self):
        self.window.BringToFront()

    def close(self):
        self.active = False
        self.window.Close()


class SubtitleWindow:

    def __init__(self, d):
        self.d = d
        self.window = None
        self.active = True
        self.subtitles = {}

        self.setup()

    def setup(self):
        # subtitles will be in a dictionary, example:
        # {'en': [{'url': 'http://x.com/s1', 'ext': 'srv1'}, {'url': 'http://x.com/s2', 'ext': 'vtt'}], 'ar': [{'url': 'https://www.youtub}, {},...]
        # template: subtitles = {language1:[sub1, sub2, ...], language2: [sub1, ...]}, where sub = {'url': 'xxx', 'ext': 'xxx'}

        # build subtitles from self.d.subtitles and self.d.automatic_captions, and rename repeated keys
        subtitles = {}
        for k, v in self.d.subtitles.items():
            if k in subtitles:
                k = k + '_2'
            subtitles[k] = v

        for k, v in self.d.automatic_captions.items():
            if k in subtitles:
                k = k + '_2'
            subtitles[k] = v

        # build gui layout
        layout = [[sg.T('Subtitles for:')], [sg.T(self.d.name, tooltip=self.d.name)]]

        for i, lang in enumerate(subtitles):
            lang_subs = subtitles[lang]  # list with sub1, sub2, ...  every sub is dict with url and ext

            extensions = [sub.get('ext', '-') for sub in lang_subs]

            # choose default extension
            if 'srt' in extensions:
                default_ext = 'srt'
            elif 'vtt' in extensions:
                # add 'srt' extension
                vtt_sub = [sub for sub in lang_subs if sub.get('ext') == 'vtt'][0]
                srt_sub = vtt_sub.copy()
                srt_sub['ext'] = 'srt'
                lang_subs.append(srt_sub)
                extensions.insert(0, 'srt')
                default_ext = 'srt'
            else:
                default_ext = extensions[0]

            layout.append([sg.Checkbox(lang, key=f'lang_{i}', size=(15, 1)), sg.T(' - Extension:'),
                           sg.Combo(values=extensions, default_value=default_ext, key=f'ext_{i}', size=(10, 1)),
                           sg.T('*sub' if lang in self.d.subtitles else '*caption')])

        layout = [[sg.Column(layout, scrollable=True, vertical_scroll_only=True, size=(433, 195), key='col')],
                  [sg.Button('Download'), sg.Button('Close')]]

        window = sg.Window('Subtitles window', layout, finalize=True)
        self.window = window
        self.subtitles = subtitles

        # set focus on first checkbox, button focus is not looking good
        self.window['lang_0'].set_focus()

    def focus(self):
        self.window.BringToFront()

    def run(self):

        event, values = self.window.read(timeout=10, timeout_key='_TIMEOUT_')

        if event in ('Close', None):
            self.close()

        elif event == 'Download':
            self.close()

            # get selected subs,
            # subtitles = {language1:[sub1, sub2, ...], language2: [sub1, ...]}, where sub = {'url': 'xxx', 'ext': 'xxx'}
            for i, lang in enumerate(self.subtitles):
                if values[f'lang_{i}']:  # selected language checkbox, true if selected
                    # get selected extension
                    ext = values[f'ext_{i}']

                    # language subs list
                    lang_subs = self.subtitles[lang]

                    # get url
                    url = [sub['url'] for sub in lang_subs if sub['ext'] == ext][0]

                    download_sub(lang, url, ext, self.d)

    def close(self):
        self.window.close()
        self.active = False


class AboutWindow:

    def __init__(self):
        self.active = True  # if False, object will be removed from "active windows list"

        # create gui
        msg1 = f'PyIDM is an open source multi-connections download manager, it downloads general files, support \n' \
               f'downloading videos, and playlists from youtube, and other media websites.\n' \
               f'Developed in Python, based on "pyCuRL/LibCurl", "youtube_dl", and "PySimpleGUI"  \n\n' \
               f'This application is free for use, in hope to be useful for someone, \n' \
               f'Conditions and usage restrictions:\n' \
               f'- The following contents are not allowed to be downloaded using this application: \n' \
               f'     - DRM "Digital rights management", protected videos / streams or Copyright materials. \n' \
               f'     - Porn videos/streams or any pornography materials\n' \
               f'     - Illegal contents or any material that encourage/promote violence or criminal/illegal behaviours\n' \
               f'- This application is provided "AS IS" without any warranty, it is under no circumstances the PyIDM author \n' \
               f'  could be held liable for any claim, or damages or responsible for any misuse of this application.\n\n' \
               f'your feedback is most welcomed on:'

        msg2 = f'Author,\n' \
               f'Mahmoud Elshahat\n' \
               f'2019-2020'

        layout = [[sg.T(msg1)],
                  [sg.T('', font='any 1')],
                  [sg.T('Home page:', size=(10, 1)), sg.T('https://github.com/pyIDM/pyIDM', key='home_page', font='any 10 underline', enable_events=True)],
                  [sg.T('Issues:', size=(10, 1)),
                   sg.T('https://github.com/pyIDM/pyIDM/issues', key='issues', font='any 10 underline',
                        enable_events=True)],

                  [sg.T('Report a bug:', size=(10, 1)), sg.T('https://github.com/pyIDM/pyIDM/issues/new', key='new_issue', font='any 10 underline',
                                               enable_events=True), sg.T('*requires github account', font='any 8')],
                  [sg.T('Email:', size=(10, 1)), sg.T('info.pyidm@gmail.com', key='email', font='any 10 underline', enable_events=True)],
                  [sg.T('', font='any 1')],
                  [sg.T(msg2)],
                  [sg.Column([[sg.Ok()]], justification='right')]]

        window = sg.Window(f'about PyIDM', layout, finalize=True)

        # set cursor for links
        for key in ('home_page', 'issues', 'new_issue', 'email'):
            window[key].set_cursor('hand2')

        self.window = window

    def focus(self):
        self.window.BringToFront()

    def run(self):
        # read events
        event, values = self.window.read(timeout=10, timeout_key='_TIMEOUT_')

        if event in ('Ok', None):
            self.close()

        elif event == 'home_page':
            webbrowser.open_new('https://github.com/pyIDM/pyIDM')

        elif event == 'issues':
            webbrowser.open_new('https://github.com/pyIDM/pyIDM/issues?q=is%3Aissue+')

        elif event == 'new_issue':
            webbrowser.open_new('https://github.com/pyIDM/pyIDM/issues/new')

        elif event == 'email':
            clipboard.copy('info.pyidm@gmail.com')
            sg.PopupOK('Email "info.pyidm@gmail.com" has been copied to clipboard\n')

    def close(self):
        self.active = False
        self.window.Close()


class PlaylistWindow:
    def __init__(self, playlist):
        self.active = True
        self.playlist = playlist  # reference to MainWindow instant
        self.window = None
        self.selected_videos = []
        self.active_threads = []  # threads
        self.video_checkboxes = []
        self.stream_combos = []
        self.progress_bars = []
        self.process_q = Queue()  # add videos which needs to fetch their streams
        self.subtitles = []  # unique subtitles names only for all videos
        self.selected_subs = []  # subtitles names only

        self.timer1 = 0

        self.setup()

    def setup(self):

        # technical limitation of tkinter, can not show more than 1000 item without glitches, or pl_window will not show
        if len(self.playlist) > 1000:
            popup('Playlist is more than 1000 videos, \n'
                  'due to technical limitations will show only first 1000 videos', title='Playlist download')
            playlist = self.playlist[:1000]
        else:
            playlist = self.playlist

        # fix repeated video names in playlist --------------------------------------------------------------------
        vid_names = []
        for num, vid in enumerate(playlist):
            if vid.name in vid_names:
                name, ext = os.path.splitext(vid.name)
                name = f'{name}_{num}{ext}'
                vid.name = name

            vid_names.append(vid.name)
        del vid_names  # no longer needed, free memory

        self.subtitles = self.update_subtitles()

        # gui layout ------------------------------------------------------------------------------------------------
        video_checkboxes = []
        progress_bars = []
        stream_combos = []

        master_stream_menu = self.create_master_menu()

        general_options_layout = [sg.Checkbox('Select All', enable_events=True, key='Select All'),
                                  sg.T('', size=(6, 1)),
                                  sg.Checkbox('Subtitles:', key='use_sub'),
                                  sg.B('', image_data=browse_icon, key='sub_btn', **transparent, tooltip=' Select Subtitles '),
                                  sg.T('', size=(7, 1)),
                                  sg.T('Master Quality:'),
                                  sg.Combo(values=master_stream_menu, default_value=master_stream_menu[0], size=(28, 1),
                                           key='master_stream_combo', enable_events=True),
                                  ]

        video_layout = []

        # build layout widgets
        for num, vid in enumerate(playlist):
            # set selected stream
            if vid.all_streams:
                vid.select_stream(index=0)

            # video names with check boxes
            video_checkbox = sg.Checkbox(truncate(vid.title, 62), size=(55, 1), tooltip=vid.title, font='any 8',
                                         key=f'video {num}', enable_events=True)
            video_checkboxes.append(video_checkbox)

            # hidden progress bars works only while loading streams
            progress_bar = sg.ProgressBar(100, size=(10, 5), pad=(5, 1), key=f'bar {num}')
            progress_bars.append(progress_bar)

            # streams / quality menu
            stream_combo = sg.Combo(values=vid.stream_menu, default_value=vid.stream_menu[1], font='any 8',
                                    size=(30, 1), key=f'stream {num}', enable_events=True, pad=(5, 0))
            stream_combos.append(stream_combo)

            # build one row from the above
            row = [video_checkbox, sg.Col([[stream_combo], [progress_bar]]),
                   sg.T(size_format(vid.total_size), size=(10, 1), font='any 8', key=f'size_text {num}')]

            # add row to video_layout
            video_layout.append(row)

        video_layout = [sg.Column(video_layout, scrollable=True, vertical_scroll_only=True, size=(650, 250), key='col')]

        layout = [
            [sg.T(f'Total Videos: {len(playlist)}')],
            general_options_layout,
            [sg.T('NOTE: Select videos first to load stream menu'), sg.T('◀◀', key='note')],  #
            [sg.Frame(title='Videos:', layout=[video_layout])],
            [sg.Col([[sg.B('Download'), sg.Cancel()]], justification='right')]
        ]

        # create window
        window = sg.Window(title='Playlist download window', layout=layout, finalize=True, margins=(2, 2))

        # set progress bar properties
        for bar in progress_bars:
            bar.Widget.config(mode='indeterminate')
            bar.expand(expand_x=True)

        self.window = window
        self.playlist = playlist
        self.video_checkboxes = video_checkboxes
        self.stream_combos = stream_combos
        self.progress_bars = progress_bars

    def focus(self):
        self.window.BringToFront()

    def create_master_menu(self):
        # extract video quality (abr for audio and height for video) ----------------------------------------------
        def extract_quality(text=''):
            # example: '      ›  mp4 - 1080'
            quality = text.rsplit(' - ', maxsplit=1)[-1]
            try:
                quality = int(quality)
            except:
                quality = 0
            finally:
                return quality

        # lists for raw names
        names_map = {'mp4_videos': [], 'other_videos': [], 'audio_streams': [], 'extra_streams': []}

        for vid in self.playlist:
            for key in names_map:
                for name in vid.names_map[key]:
                    # convert name to raw name ex:    › mp4 - 1080 - 29.9 MB - id:137   to      › mp4 - 1080
                    name = ' - '.join(name.split(' - ')[:2])
                    if name not in names_map[key]:
                        names_map[key].append(name)

        # sort names based on quality
        for key in names_map:
            names_map[key] = sorted(names_map[key], key=extract_quality, reverse=True)

        # build master combo box
        master_stream_menu = ['● Video streams:                     '] + names_map['mp4_videos'] + names_map[
            'other_videos'] + \
                             ['', '● Audio streams:                 '] + names_map['audio_streams'] + \
                             ['', '● Extra streams:                 '] + names_map['extra_streams']

        return master_stream_menu

    def update_subtitles(self):
        # subtitles names
        c = set()
        for d in self.playlist:
            for k, v in d.subtitles.items():
                c.add(k)

            for k, v in d.automatic_captions.items():
                c.add(k)

        subtitles = sorted(list(c))
        return subtitles

    def update_video(self, num):
        # update some parameters for a selected video
        vid = self.playlist[num]
        stream_widget = self.window[f'stream {num}']
        size_widget = self.window[f'size_text {num}']

        stream_text = stream_widget.get()

        # first check if video has streams
        if not vid.all_streams:
            return

        # correct chosen stream values
        stream = vid.select_stream(name=stream_text)
        if not stream:
            stream_widget(vid.selected_stream.name)
        size_widget(size_format(vid.total_size))

    def follow_master_selection(self, num):
        """
        set stream menu selection for current video to match master stream menu selection
        :param num: int, video number
        :return: None
        """
        master_combo = self.window['master_stream_combo']
        master_text = master_combo.get()  # example: mp4 - 1080

        vid_combo = self.stream_combos[num]
        vid = self.playlist[num]

        # set a matching stream name, note: for example, if master_text is "mp4 - 1080",
        # then matching could be "mp4 - 1080 - 30 MB - id:137" with size and format id included
        match_stream_name = [text for text in vid.stream_menu if master_text in text]
        if match_stream_name:
            match_stream_name = match_stream_name[0]
            vid_combo(match_stream_name)

            self.update_video(num)

    def run(self):
        # event loop -------------------------------------------------------------------------------------------------
        playlist = self.playlist

        # while True:
        event, values = self.window.read(timeout=100)
        if event in (None, 'Cancel') or config.terminate:
            self.close()

        elif event == 'sub_btn':
            if not self.subtitles:
                sg.popup_ok('There is no subtitles available!')
                return

            col = sg.Col([[
                sg.Checkbox(sub_name, key=sub_name, default=True if sub_name in self.selected_subs else False)]
                for sub_name in self.subtitles], size=(180, 200), scrollable=True, vertical_scroll_only=True)
            sub_window = sg.Window('Select Subtitles', [[col], [sg.Ok(), sg.Cancel()]], keep_on_top=True)
            sub_event, _ = sub_window()
            if sub_event == 'Ok':
                self.selected_subs = []
                for sub_name in self.subtitles:
                    checked = sub_window[sub_name].get()
                    if checked:
                        self.selected_subs.append(sub_name)

                log('selected subs:', self.selected_subs)

                # tick checkbox based on selected subs
                self.window['use_sub'](True if self.selected_subs else False)

            sub_window.close()

        elif event == 'Download':
            self.selected_videos.clear()
            null_videos = []
            for num, vid in enumerate(playlist):
                # check if video is selected
                selected = values[f'video {num}']

                if selected:
                    # append to chosen videos list
                    self.selected_videos.append(vid)

                    # get selected text from stream menu
                    selected_text = values[f'stream {num}']

                    # get selected stream
                    stream = vid.select_stream(name=selected_text)

                    # select subtitle
                    if self.window['use_sub'].get():
                        vid.select_subs(self.selected_subs)

                    # check if video has streams or not
                    if not stream:
                        null_videos.append(vid.name)

            # do nothing if there is selected videos which have no streams
            if null_videos:
                vid_names = "\n".join(null_videos)
                sg.popup_ok(f'The following selected videos: \n\n'
                            f'{vid_names}\n\n'
                            f'have no streams, please wait until finish loading '
                            f'or un-select this video and try again')
            elif self.selected_videos:
                # downloading
                Thread(target=self.download_selected_videos).start()

                # select downloads tab
                execute_command("select_tab", tab_name='Downloads')

                self.close()

            else:  # no selected videos, warn user before close
                sg.popup_ok('There is no videos selected', 'choose videos first or hit cancel to close window')

        elif event == 'Select All':
            checked = values['Select All']

            # process all other check boxes
            for num, checkbox in enumerate(self.video_checkboxes):
                checkbox(checked)
                vid = self.playlist[num]

                # fetch video info if not processed
                if checked and not vid.processed:
                    self.process_q.put(vid)

        elif event == 'master_stream_combo':
            for num, _ in enumerate(self.stream_combos):
                self.follow_master_selection(num)

        # video checkbox
        elif event.startswith('video'):
            num = int(event.split()[-1])
            vid = self.playlist[num]
            checked = values[event]

            # fetch video info if not processed
            if checked and not vid.processed:
                self.process_q.put(vid)

        # stream menu events
        elif event.startswith('stream'):
            num = int(event.split()[-1])
            self.update_video(num)

        if self.active:
            # update stream menu for processed videos, in case stream menu not yet loaded
            for num, vid in enumerate(playlist):
                stream_combo = self.window[f'stream {num}']
                if vid.all_streams:
                    if stream_combo.Values != vid.stream_menu:
                        stream_combo(values=vid.stream_menu)

                        # set current selection to match master combo selection
                        self.follow_master_selection(num)

                        # should update master stream combo
                        master_stream_text = values['master_stream_combo']
                        self.window['master_stream_combo'](values=self.create_master_menu(), value=master_stream_text)

                        # update subtitles
                        self.subtitles = self.update_subtitles()

            # animate progress bars while loading streams
            for num, bar in enumerate(self.progress_bars):
                vid = playlist[num]
                if vid.busy:
                    bar(visible=True)
                    bar.expand(expand_x=True)
                    bar.Widget['value'] += 10
                else:
                    bar(visible=False)

            # run every (n) second
            if time.time() - self.timer1 >= 1:
                self.timer1 = time.time()

                # animate "note" widget
                flip_visibility(self.window['note'])

                # fetch video streams info
                self .active_threads = [t for t in self.active_threads if t.is_alive()]
                if self.process_q.qsize() and len(self.active_threads) < 10:
                    vid = self.process_q.get()
                    t = Thread(target=process_video_info, daemon=True, args=(vid,))
                    self.active_threads.append(t)
                    t.start()

    def download_selected_videos(self):
        for vid in self.selected_videos:
            log(f'download playlist fn> {repr(vid.selected_stream)}, title: {vid.name}')
            vid.folder = config.download_folder

            # send download request to main window
            execute_command("start_download", vid, silent=True)

            # give a small pause intervals to prevent cpu surge
            time.sleep(0.5)

    def close(self):
        # set in active status
        self.active = False

        self.window.close()


class UpdateWindow:

    def __init__(self, update_description):
        self.active = True  # if False, object will be removed from "active windows list"
        self.update_description = update_description
        self.window = None

    def setup(self):

        # create gui
        buttons = []
        if config.FROZEN: # show update button for Frozen versions only i.e. "windows portable version"
            buttons.append(sg.B('Update'))

        buttons += [sg.B('website'), sg.Cancel()]

        layout = [
            [sg.T('New update available:')],
            [sg.Multiline(self.update_description, size=(70, 10))],
            buttons
        ]

        self.window = sg.Window('Update Application', layout, finalize=True, keep_on_top=True)

    def focus(self):
        self.window.BringToFront()

    def run(self):

        # using this technique we can start gui window from a thread
        if not self.window:
            self.setup()

        # read events
        event, values = self.window.read(timeout=10, timeout_key='_TIMEOUT_')

        if event == 'Update':
            Thread(target=update.update).start()
            execute_command('select_tab', 'Log')

        elif event == 'website':
            update.open_update_link()

        if event != '_TIMEOUT_':
            self.close()

    def close(self):
        self.active = False
        self.window.Close()


class SysTray:
    """
    systray icon using pystray package
    """
    def __init__(self):
        self._active = False  # if systray works correctly
        self._tray_icon_path = os.path.join(config.sett_folder, 'systray.png')  # path to icon
        self.icon = None
        self._hover_text = None
        self.Gtk = None

    @property
    def active(self):
        return self._active

    @active.setter
    def active(self, value):
        self._active = value
        config.systray_active = value

    @staticmethod
    def show_main_window(*args):
        config.main_q.put('start_main_window')

    @staticmethod
    def minimize_to_systray(*args):
        config.main_q.put('minimize_to_systray')

    @staticmethod
    def close_to_systray(*args):
        config.main_q.put('close_to_systray')

    @property
    def tray_icon(self):
        """path to icon file"""
        try:
            # read base64 icon string into io buffer
            buffer = io.BytesIO(base64.b64decode(APP_ICON2))

            # open buffer by Pillow
            img = Image.open(buffer)

            # free memory
            # buffer.close()

            return img
        except Exception as e:
            log('systray: tray_icon', e)
            if config.TEST_MODE:
                raise e

    @property
    def tray_icon_path(self):
        # save icon as a png on setting directory and return path
        if not os.path.isfile(self._tray_icon_path):
            try:
                # save file to settings folder
                self.tray_icon.save(self._tray_icon_path, format='png')
            except:
                pass

        return self._tray_icon_path

    def run(self):
        # not supported on mac
        if config.operating_system == 'Darwin':
            log('Systray is not supported on mac yet')
            return

        # make our own Gtk statusIcon, since pystray failed to run icon properly on Gtk 3.0 from a thread
        if config.operating_system == 'Linux':
            try:
                import gi
                gi.require_version('Gtk', '3.0')
                from gi.repository import Gtk
                self.Gtk = Gtk

                def icon_right_click(icon, button, time):
                    menu = Gtk.Menu()

                    item1 = Gtk.MenuItem(label="Start / Show")
                    item2 = Gtk.MenuItem(label='Minimize to Systray')
                    item3 = Gtk.MenuItem(label='Close to Systray')
                    item4 = Gtk.MenuItem(label='Quit')

                    item1.connect('activate', self.show_main_window)
                    item2.connect('activate', self.minimize_to_systray)
                    item3.connect('activate', self.close_to_systray)
                    item4.connect('activate', self.quit)

                    for item in(item1, item2, item3, item4):
                        menu.append(item)

                    menu.show_all()
                    menu.popup(None, None, None, icon, button, time)

                icon = Gtk.StatusIcon()
                icon.set_from_file(self.tray_icon_path)
                icon.connect("popup-menu", icon_right_click)
                icon.connect('activate', self.show_main_window)

                self.active = True
                Gtk.main()
                return
            except:
                self.active = False

        # let pystray decide which icon to run
        try:
            from pystray import Icon, Menu, MenuItem
            menu = Menu(MenuItem("Start / Show", self.show_main_window, default=True),
                        MenuItem("Minimize to Systray", self.minimize_to_systray),
                        MenuItem("Close to Systray", self.close_to_systray),
                        MenuItem("Quit", self.quit))
            self.icon = Icon('PyIDM', self.tray_icon, menu=menu)
            self.active = True
            self.icon.run()
        except Exception as e:
            log('systray: - run() - ', e)
            self.active = False

    def update(self, hover_text=None, icon=None):
        pass

    def shutdown(self):
        try:
            self.icon.stop()
        except:
            pass

        try:
            # quit main, don't know why it raise (Gtk-CRITICAL **:gtk_main_quit: assertion 'main_loops != NULL' failed)
            # but it has no side effect and PyIDM quit normally
            self.Gtk.main_quit()
        except:
            pass

    def quit(self, *args):
        """callback when selecting quit from systray menu"""
        # set global terminate flag
        self.shutdown()
        self.active = False
        config.terminate = True
        config.shutdown = True


