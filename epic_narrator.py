import faulthandler
import os
import queue
import sys
import ctypes
import time
import traceback
import argparse
import vlc
import numpy as np
import matplotlib
from settings import Settings

matplotlib.use('PS')
import matplotlib.pyplot as plt
import gi
from recorder import Recorder
from recordings import Recordings, ms_to_timestamp
from unique_queue import UniqueQueue

gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, GLib, Gdk, Pango
from matplotlib.animation import FuncAnimation
from matplotlib.backends.backend_gtk3agg import (FigureCanvasGTK3Agg as FigureCanvas)
from threading import Thread, Event, Timer

if sys.platform.startswith('darwin'):
    plt.switch_backend('MacOSX')
else:
    plt.switch_backend('GTK3Agg')


class EpicNarrator(Gtk.ApplicationWindow):
    def __init__(self, mic_device=0):
        Gtk.ApplicationWindow.__init__(self, title='Epic Narrator')
        gtk_settings = Gtk.Settings.get_default()
        gtk_settings.set_property("gtk-application-prefer-dark-theme", False)

        self.ui_ready = False
        self.settings = Settings()
        self.player = None
        self.rec_player = None
        self.video_length_ms = 0
        self.seek_step = 500  # 500ms
        self.red_tick_colour = "#ff3300"
        self.video_width = 900
        self.video_height = 400
        self.connect('destroy', Gtk.main_quit)

        self.recorder = self.set_mic(mic_device)
        hold_to_record = self.settings.get_setting('hold_to_record')
        self.hold_to_record = False if hold_to_record is None else hold_to_record
        self.settings.update_settings(hold_to_record=self.hold_to_record)

        self.recordings = None
        self.video_path = None
        self.is_video_loaded = False
        self.annotation_box_map = {}
        self.single_window = False if sys.platform.startswith('darwin') else True
        self.annotation_box_height = self.video_height if self.single_window else 200
        self._timeout_id_backwards = 0
        self._timeout_id_forwards = 0
        self.was_playing_before_seek = None
        self.is_seeking = False
        self.play_recs_with_video = False

        # menu
        self.file_menu = Gtk.Menu()
        self.load_video_menu_item = Gtk.MenuItem(label='Load video')
        self.file_menu.append(self.load_video_menu_item)
        self.file_menu_item = Gtk.MenuItem(label='File')
        self.file_menu_item.set_submenu(self.file_menu)
        self.menu_bar = Gtk.MenuBar()
        self.menu_bar.append(self.file_menu_item)
        self.load_video_menu_item.connect('button-press-event', self.choose_video)
        self.set_microphone_menu()

        self.settings_menu = Gtk.Menu()
        self.hold_to_record_menu_item = Gtk.CheckMenuItem(label='Hold to record')
        self.hold_to_record_menu_item.set_active(self.hold_to_record)
        self.hold_to_record_menu_item.connect('toggled', self.hold_to_record_toggled)
        self.settings_menu.append(self.hold_to_record_menu_item)
        self.settings_menu_item = Gtk.MenuItem(label='Settings')
        self.settings_menu_item.set_submenu(self.settings_menu)
        self.menu_bar.append(self.settings_menu_item)

        # button icons
        self.seek_backward_image = Gtk.Image.new_from_icon_name('media-seek-backward', Gtk.IconSize.BUTTON)
        self.seek_forward_image = Gtk.Image.new_from_icon_name('media-seek-forward', Gtk.IconSize.BUTTON)
        self.play_image = Gtk.Image.new_from_icon_name('media-playback-start', Gtk.IconSize.BUTTON)
        self.pause_image = Gtk.Image.new_from_icon_name('media-playback-pause', Gtk.IconSize.BUTTON)
        self.mute_image = Gtk.Image.new_from_icon_name('audio-volume-muted', Gtk.IconSize.BUTTON)
        self.unmute_image = Gtk.Image.new_from_icon_name('audio-volume-high', Gtk.IconSize.BUTTON)
        self.mic_image = Gtk.Image.new_from_icon_name('audio-input-microphone', Gtk.IconSize.BUTTON)
        self.record_image = Gtk.Image.new_from_icon_name('media-record', Gtk.IconSize.BUTTON)

        # slider
        self.slider = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=None)
        self.slider.connect('change-value', self.slider_moved)
        self.slider.connect('button-press-event', self.slider_clicked)
        self.slider.connect('button-release-event', self.slider_released)
        self.slider.set_hexpand(True)
        self.slider.set_valign(Gtk.Align.CENTER)
        self.slider.set_draw_value(False)

        # buttons
        self.playback_button = Gtk.Button()
        self.record_button = Gtk.Button()
        self.mute_button = Gtk.Button()
        self.seek_backward_button = Gtk.Button()
        self.seek_forward_button = Gtk.Button()
        self.playback_button.set_image(self.play_image)
        self.record_button.set_image(self.mic_image)
        self.mute_button.set_image(self.unmute_image)
        self.seek_backward_button.set_image(self.seek_backward_image)
        self.seek_forward_button.set_image(self.seek_forward_image)
        self.seek_backward_button.connect('pressed', self.seek_backwards_pressed)
        self.seek_backward_button.connect('released', self.seek_backwards_released)
        self.seek_forward_button.connect('pressed', self.seek_forwards_pressed)
        self.seek_forward_button.connect('released', self.seek_forwards_released)
        self.playback_button.connect('clicked', self.toggle_player_playback)
        self.record_button.connect('pressed', self.record_button_clicked)
        self.record_button.connect('released', self.record_button_released)
        self.mute_button.connect('clicked', self.toggle_audio)

        # video area
        self.video_area = Gtk.DrawingArea() if self.single_window else Gtk.Window(title='Epic Narrator')
        self.video_area.set_size_request(self.video_width, self.video_height)
        self.video_area.connect('realize', self.video_area_ready)

        # time label
        self.time_label = Gtk.Label()
        self.update_time_label(0)

        self.speed_time_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)

        # speed radio buttons
        speed_item = None
        self.normal_speed_button = None
        speeds = [0.50, 0.75, 1, 1.50, 2]

        self.speed_time_box.pack_start(Gtk.Label(label='Playback speed'), False, False, 10)

        for speed in speeds:
            speed_item = Gtk.RadioButton(label='{:0.2f}'.format(speed), group=speed_item)
            speed_item.connect('clicked', self.speed_selected, speed)
            speed_item.set_can_focus(False)

            if speed == 1:
                speed_item.set_active(True)
                self.normal_speed_button = speed_item

            self.speed_time_box.pack_start(speed_item, False, False, 0)

        self.play_recs_with_video_button = Gtk.CheckButton(label='Play recordings with video')
        self.play_recs_with_video_button.connect('toggled', self.play_recs_with_video_toggled)

        self.speed_time_box.pack_end(self.time_label, False, False, 0)
        self.speed_time_box.pack_end(self.play_recs_with_video_button, False, False, 5)

        # button box
        self.button_box = Gtk.ButtonBox()
        self.button_box.pack_start(self.seek_backward_button, False, False, 0)
        self.button_box.pack_start(self.seek_forward_button, False, False, 0)
        self.button_box.pack_start(self.playback_button, False, False, 0)
        self.button_box.pack_start(self.record_button, False, False, 0)
        self.button_box.pack_start(self.mute_button, False, False, 0)
        self.button_box.set_spacing(10)
        self.button_box.set_layout(Gtk.ButtonBoxStyle.CENTER)

        # microphone monitor
        self.monitor_fig, self.monitor_ax, self.monitor_lines = self.recorder.prepare_monitor_fig()
        self.recorder_plot_data = np.zeros((self.recorder.length, len(self.recorder.channels)))
        canvas = FigureCanvas(self.monitor_fig)  # a Gtk.DrawingArea
        canvas.set_size_request(100, 50)
        self.monitor_label = Gtk.Label()
        self.set_monitor_label(False)
        self.monitor_animation = FuncAnimation(self.monitor_fig, self.update_mic_monitor,
                                               interval=self.recorder.plot_interval_ms, blit=True)

        # annotation box
        self.right_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.annotation_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.annotation_scrolled_window = Gtk.ScrolledWindow()
        self.annotation_scrolled_window.set_border_width(10)
        self.annotation_scrolled_window.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.annotation_scrolled_window.add_with_viewport(self.annotation_box)
        self.right_box.pack_start(Gtk.Label(label='Recordings'), False, False, 10)
        self.right_box.pack_start(self.annotation_scrolled_window, True, True, 0)
        self.right_box.set_size_request(300, self.annotation_box_height)
        self.highlighted_recording_button = None
        self.highlighed_recording_time = None

        # self.annotation_box.connect('size-allocate', self.scroll_annotations_to_bottom)

        self.video_path_label = Gtk.Label(label=' ')
        self.recordings_path_label = Gtk.Label(label=' ')

        # video box
        self.video_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.video_box.pack_start(self.menu_bar, False, False, 0)

        if self.single_window:
            self.video_box.pack_start(self.video_area, True, True, 0)
        else:
            self.video_area.show()
            self.video_area.move(0, 0)
            self.move(0, self.video_height+100)

            # enable only horizontal resize
            gh = Gdk.Geometry()
            gh.max_height = 300
            gh.min_height = 300
            gh.max_width = 2000
            gh.min_width = 900
            self.set_geometry_hints(None, gh, Gdk.WindowHints.MAX_SIZE)

        self.video_box.pack_start(self.speed_time_box, False, False, 10)
        self.video_box.pack_start(self.slider, False, False, 0)
        self.video_box.pack_start(self.button_box, False, False, 20)
        self.video_box.pack_start(self.monitor_label, False, False, 0)
        self.video_box.pack_start(canvas, False, False, 10)

        # bottom paths labels
        self.paths_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)

        for path_labels in [self.video_path_label, self.recordings_path_label]:
            path_labels.set_property('lines', 1)
            path_labels.set_ellipsize(Pango.EllipsizeMode.START)
            path_labels.set_property('max-width-chars', 50)

        video_path_placeholder = Gtk.Label()
        video_path_placeholder.set_markup('<span><b>Annotating video:</b></span>')
        recordings_path_placeholder = Gtk.Label()
        recordings_path_placeholder.set_markup('<span><b>Saving recordings to:</b></span>')
        self.paths_box.pack_start(video_path_placeholder, False, False, 10)
        self.paths_box.pack_start(self.video_path_label, False, False, 0)
        self.paths_box.pack_end(self.recordings_path_label, False, False, 0)
        self.paths_box.pack_end(recordings_path_placeholder, False, False, 10)

        self.video_box.pack_start(self.paths_box, False, False, 10)

        self.main_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.main_box.pack_start(self.video_box, False, True, 0)
        self.main_box.pack_start(self.right_box, False, True, 0)

        self.add(self.main_box)

        # initial setup
        self.recorder.stream.start()  # starts the microphone stream
        self.toggle_media_controls(False)
        self.record_button.set_sensitive(False)
        self.mute_button.set_sensitive(False)

        self.connect("key-press-event", self.key_pressed)
        self.connect("key-release-event", self.key_released)

        # queue to play recordings with video
        self.rec_queue = UniqueQueue()  # writer() writes to rec_queue from _this_ process
        self.rec_worker = Thread(target=self.rec_reader_proc, args=(self.rec_queue,))
        self.rec_worker.setDaemon(True)
        self.rec_playing_event = Event()
        self.rec_playing_event.clear()
        self.rec_worker.start()
        self.last_played_rec = None
        self.ui_ready = True

    def set_mic(self, default_mic_device):
        saved_microphone = self.settings.get_setting('microphone')
        mic_id = saved_microphone if saved_microphone is not None else default_mic_device

        try:
            recorder = Recorder(device_id=mic_id)
        except Exception:
            recorder = Recorder(device_id=default_mic_device)
            dialog = Gtk.MessageDialog(self, 0, Gtk.MessageType.ERROR, Gtk.ButtonsType.OK, 'Cannot use this device')
            dialog.format_secondary_text('Could not use device with ID {}. This is likely due to a saved configuration '
                                         'that is no longer available '
                                         '(e.g. you used a device that is not plugged anymore)\n\n'
                                         'Using default mic with ID {} now'.format(mic_id, default_mic_device))
            dialog.run()
            dialog.destroy()
            self.settings.update_settings(microphone=default_mic_device)

        return recorder

    def rec_reader_proc(self, queue):
        while True:
            if not self.rec_playing_event.is_set():
                self.rec_playing_event.wait()

            time_ms = queue.get()
            self.play_recording(None, None, time_ms)

    def play_recs_with_video_toggled(self, widget):
        self.play_recs_with_video = self.play_recs_with_video_button.get_active()

    def finished_playing_recording(self, args):
        self.rec_playing_event.set()

    def set_monitor_label(self, is_recording):
        colour = '#ff3300' if is_recording else 'black'
        self.monitor_label.set_markup('<span foreground="{}">Microphone level</span>'.format(colour))

    def speed_selected(self, widget, speed):
        if self.is_video_loaded:
            self.player.set_rate(speed)

    def set_focus(self):
        widgets = [self.main_box, self.slider, self.video_box, self.button_box,
                   self.speed_time_box, self.seek_backward_button, self.seek_forward_button, self.playback_button,
                   self.record_button, self.mute_button, self.annotation_box, self.play_recs_with_video_button, self]

        for w in widgets:
            w.set_can_focus(False)

    def key_released(self, widget, event):
        if not self.is_video_loaded:
            return True

        if event.keyval == Gdk.KEY_Left:
            self.seek_backwards_released()
        elif event.keyval == Gdk.KEY_Right:
            self.seek_forwards_released()
        elif event.keyval == Gdk.KEY_Return:
            if self.hold_to_record:
                if self.recorder.is_recording:
                    self.stop_recording()
            else:
                self.toggle_record()
        else:
            return True

    def key_pressed(self, widget, event):
        if not self.is_video_loaded:
            return True

        if event.keyval == Gdk.KEY_Left:
            self.seek_backwards_pressed()
        elif event.keyval == Gdk.KEY_Right:
            self.seek_forwards_pressed()
        elif event.keyval == Gdk.KEY_space:
            self.toggle_player_playback()
        elif event.keyval == Gdk.KEY_M or event.keyval == Gdk.KEY_m:
            self.toggle_audio()
        elif event.keyval == Gdk.KEY_Return:
            if self.hold_to_record:
                if not self.recorder.is_recording:
                    self.start_recording()
        elif event.keyval == Gdk.KEY_Delete or event.keyval == Gdk.KEY_BackSpace:
            if self.recordings.empty():
                pass

            if self.player.is_playing():
                paused = True
                self.pause_video()
            else:
                paused = False

            if self.recorder.is_recording:
                self.stop_recording(play_afterwards=paused)
                current_recording = True
            else:
                current_recording = False

            if self.highlighed_recording_time is not None:
                self.delete_recording(self.highlighted_recording_button, None, self.highlighed_recording_time,
                                      current_recording=current_recording)

            if paused:
                self.play_video()
        else:
            pass

        return True

    def show(self):
        self.show_all()

    def hold_to_record_toggled(self, args):
        self.hold_to_record = self.hold_to_record_menu_item.get_active()
        self.settings.update_settings(hold_to_record=self.hold_to_record)

    def add_annotation_box(self, time_ms):
        box = Gtk.ButtonBox()

        time_button = Gtk.Button()
        time_label = Gtk.Label()
        time_label.set_markup('<span foreground="black"><tt>{}</tt></span>'.format(ms_to_timestamp(time_ms)))
        time_button.add(time_label)
        # time_label.show_all()

        a_play_button = Gtk.Button()
        # we need to create new images every time otherwise only the last entry will display the image
        a_play_button.set_image(Gtk.Image.new_from_icon_name('media-playback-start', Gtk.IconSize.BUTTON))
        a_delete_button = Gtk.Button()
        a_delete_button.set_image(Gtk.Image.new_from_icon_name('edit-delete', Gtk.IconSize.BUTTON))

        time_button.connect('button-press-event', self.go_to, time_ms)
        a_play_button.connect('button-press-event', self.play_recording, time_ms)
        a_delete_button.connect('button-press-event', self.delete_recording, time_ms)

        box.pack_start(time_button, False, False, 0)
        box.pack_start(a_play_button, False, False, 0)
        box.pack_start(a_delete_button, False, False, 0)
        box.set_layout(Gtk.ButtonBoxStyle.CENTER)
        box.set_spacing(5)
        box.show_all()
        box.set_can_focus(False)
        time_button.set_can_focus(False)
        a_play_button.set_can_focus(False)
        a_delete_button.set_can_focus(False)

        self.annotation_box_map[time_ms] = box
        self.annotation_box.pack_start(box, False, True, 0)
        self.refresh_annotation_box()
        self.annotation_box.show_all()

        return box

    def go_to(self, widget, event, time_ms):
        self.slider.set_value(time_ms)
        self.player.set_time(int(time_ms))
        self.update_time_label(time_ms)

        if widget is not None:
            self.highlight_recording_annotation(widget.get_parent(), time_ms)

    def scroll_annotations_to_bottom(self, *args):
        adj = self.annotation_scrolled_window.get_vadjustment()
        adj.set_value(adj.get_upper())

    def scroll_annotations_box_to_time(self, time_ms):
        rec = self.recordings.get_closest_recording(time_ms)
        box = self.annotation_box_map[rec] if rec in self.annotation_box_map else None

        if box is not None:
            adj = self.annotation_scrolled_window.get_vadjustment()
            _, y = self.annotation_box.translate_coordinates(box, 0, 0)
            adj.set_value(abs(y))
            self.highlight_recording_annotation(box, rec)

    def reset_highlighted_annotation(self):
        if self.highlighted_recording_button is not None:
            css_classes = ['destructive-action', 'suggested-action']
            context = self.highlighted_recording_button.get_style_context()

            for c in css_classes:
                context.remove_class(c)

        self.highlighed_recording_time = None
        self.highlighted_recording_button = None

    def highlight_recording_annotation(self, recording_box, time_ms, current_recording=False):
        self.reset_highlighted_annotation()

        button = recording_box.get_children()[0]
        css_class = 'destructive-action' if current_recording else 'suggested-action'
        context = button.get_style_context()
        context.add_class(css_class)
        self.highlighted_recording_button = button
        self.highlighed_recording_time = time_ms

    def play_recording(self, widget, event, time_ms):
        recording_path = self.recordings.get_path_for_recording(time_ms)

        if recording_path is not None:
            audio_media = self.vlc_instance.media_new_path(recording_path)
            self.rec_player.audio_set_mute(False)
            mrl = audio_media.get_mrl()
            self.rec_player.set_mrl(mrl)
            self.rec_playing_event.clear()
            self.rec_player.play()

    def delete_recording(self, widget, event, time_ms, current_recording=False):
        if current_recording:
            msg = 'Are you sure you want to delete the current recording?'
        else:
            msg = 'Are you sure you want to delete recording at time {}?'.format(ms_to_timestamp(time_ms))
        dialog = Gtk.MessageDialog(self, 0, Gtk.MessageType.QUESTION,
                                   (Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, Gtk.STOCK_OK, Gtk.ResponseType.OK),
                                   'Confirm delete')
        dialog.format_secondary_text(msg)
        response = dialog.run()
        dialog.destroy()

        if response == Gtk.ResponseType.OK:
            self.recordings.delete_recording(time_ms)
            self.remove_annotation_box(widget.get_parent())
            self.refresh_recording_ticks()

            if time_ms == self.highlighed_recording_time:
                self.reset_highlighted_annotation()

    def remove_annotation_box(self, widget):
        self.annotation_box_map = {key: val for key, val in self.annotation_box_map.items() if val != widget}
        self.annotation_box.remove(widget)
        self.refresh_annotation_box()

    def remove_all_annotation_boxes(self):
        for w in self.annotation_box.get_children():
            self.annotation_box.remove(w)

    def refresh_annotation_box(self):
        order = sorted(list(self.annotation_box_map.keys()))

        for time_ms, widget in self.annotation_box_map.items():
            position = order.index(time_ms)
            self.annotation_box.reorder_child(widget, position)

    def add_time_tick(self, time_ms, colour=None):
        self.slider.add_mark(time_ms, Gtk.PositionType.TOP, None)

    def add_start_end_slider_ticks(self):
        self.add_time_tick(1)
        self.add_time_tick(self.video_length_ms)

    def refresh_recording_ticks(self):
        self.slider.clear_marks()

        for time_ms in self.recordings.get_recordings_times():
            self.add_time_tick(time_ms, colour=self.red_tick_colour)

    def set_microphone_menu(self):
        devices = Recorder.get_devices()
        self.mic_menu = Gtk.Menu()
        self.mic_menu_item = Gtk.MenuItem(label='Select microphone')
        self.mic_menu_item.set_submenu(self.mic_menu)

        mic_item = None

        for dev_idx, dev in enumerate(devices):
            dev_name = dev['name']
            mic_item = Gtk.RadioMenuItem(label=dev_name, group=mic_item)
            mic_item.connect('activate', self.microphone_selected, dev_idx)

            if dev_idx == self.recorder.device_id:
                mic_item.set_active(True)

            self.mic_menu.append(mic_item)

        self.menu_bar.append(self.mic_menu_item)

    def microphone_selected(self, mic_item, mic_id):
        try:
            if self.ui_ready and mic_id != self.recorder.device_id:
                # second condition to prevent the mic to be set twice (which unfortunately happen)
                if self.recorder.is_recording:
                    self.stop_recording()

                self.recorder.change_device(mic_id)
                self.recorder.stream.start()  # starts the microphone stream
                self.settings.update_settings(microphone=mic_id)
        except Exception as e:
            traceback.print_exc()
            dialog = Gtk.MessageDialog(self, 0, Gtk.MessageType.ERROR, Gtk.ButtonsType.OK, 'Cannot use this device')
            dialog.format_secondary_text('Please select another device and check you can see a signal in the '
                                         'microphone level when you speak')
            dialog.run()
            dialog.destroy()

    def choose_video(self, *args):
        if self.is_video_loaded:
            confirm_dialog = Gtk.MessageDialog(self, 0, Gtk.MessageType.QUESTION,
                                       (Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, Gtk.STOCK_OK, Gtk.ResponseType.OK),
                                       'Confirm loading another video')
            confirm_dialog.format_secondary_text('Are you sure you want to load another video?')
            response = confirm_dialog.run()

            if response != Gtk.ResponseType.OK:
                confirm_dialog.destroy()
                return

            confirm_dialog.destroy()

        file_dialog = Gtk.FileChooserDialog("Open video", self, action=Gtk.FileChooserAction.OPEN,
                                            buttons=(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                                                     Gtk.STOCK_OK, Gtk.ResponseType.OK))

        video_file_filter = Gtk.FileFilter()
        video_file_filter.set_name("Video files")
        video_file_filter.add_mime_type("video/*")
        file_dialog.add_filter(video_file_filter)

        all_file_filter = Gtk.FileFilter()
        all_file_filter.set_name('All files')
        all_file_filter.add_pattern('*')
        file_dialog.add_filter(all_file_filter)

        saved_video_folder = self.settings.get_setting('video_folder')

        if saved_video_folder is not None and os.path.exists(saved_video_folder):
            file_dialog.set_current_folder(saved_video_folder)

        response = file_dialog.run()

        if response == Gtk.ResponseType.OK:
            path = file_dialog.get_filename()

            if os.path.isdir(path):
                message_dialog = Gtk.MessageDialog(self, 0, Gtk.MessageType.ERROR, Gtk.ButtonsType.OK, 'Invalid path')
                message_dialog.format_secondary_text('You cannot select a folder!')
                message_dialog.run()
                message_dialog.destroy()
                file_dialog.destroy()
                self.choose_video()
            else:
                file_dialog.destroy()
                self.setup(path)
        else:
            file_dialog.destroy()

    def update_mic_monitor(self, *args):
        while True:
            try:
                data = self.recorder.q.get_nowait()
            except queue.Empty:
                break

            shift = len(data)
            self.recorder_plot_data = np.roll(self.recorder_plot_data, -shift, axis=0)
            self.recorder_plot_data[-shift:, :] = data

        for column, line in enumerate(self.monitor_lines):
            line.set_ydata(self.recorder_plot_data[:, column])
            color = 'red' if self.recorder.is_recording else 'white'
            line.set_color(color)

        return self.monitor_lines

    def record_button_clicked(self, *args):
        if self.hold_to_record:
            if not self.recorder.is_recording:
                self.start_recording()
        else:
            self.toggle_record()

    def record_button_released(self, *args):
        if self.hold_to_record:
            if self.recorder.is_recording:
                self.stop_recording()

    def toggle_record(self, *args):
        if not self.recorder.is_recording:
            self.start_recording()
        else:
            self.stop_recording()

    def stop_recording(self, play_afterwards=True):
        self.record_button.set_image(self.mic_image)
        self.set_monitor_label(False)

        if not self.hold_to_record:
            time.sleep(0.5)

        self.stop_recording_proc()

        if play_afterwards:
            self.play_video(None)

    def stop_recording_proc(self, play_afterwards=True):
        self.recorder.stop_recording()
        self.toggle_media_controls(True)

    def start_recording(self):
        rec_time = self.player.get_time()

        while self.recordings.recording_exists(rec_time):
            rec_time += 1  # shifting one millisecond

        self.record_button.set_image(self.record_image)
        self.set_monitor_label(True)
        self.toggle_media_controls(False)

        if self.player.is_playing():
            self.pause_video(None)

        path = self.recordings.add_recording(rec_time)
        self.recorder.start_recording(path)
        box = self.add_annotation_box(rec_time)
        self.highlight_recording_annotation(box, rec_time, current_recording=True)
        self.add_time_tick(rec_time, colour=self.red_tick_colour)

    def toggle_media_controls(self, active):
        self.slider.set_sensitive(active)
        self.seek_backward_button.set_sensitive(active)
        self.seek_forward_button.set_sensitive(active)
        self.playback_button.set_sensitive(active)

    def seek_backwards_pressed(self, *args):
        if self.is_seeking or self._timeout_id_backwards != 0 or self._timeout_id_forwards != 0:
            return

        self.is_seeking = True
        timeout = 50

        if self.player.is_playing():
            self.player.pause()
            self.was_playing_before_seek = True
        else:
            self.was_playing_before_seek = False

        self.last_played_rec = None
        self._timeout_id_backwards = GLib.timeout_add(timeout, self.seek_backwards)

    def seek_backwards_released(self, *args):
        # remove timeout
        GLib.source_remove(self._timeout_id_backwards)
        self._timeout_id_backwards = 0

        if self.was_playing_before_seek:
            self.player.play()

        self.is_seeking = False

    def seek_backwards(self):
        seek_pos = self.slider.get_value() - self.seek_step

        if seek_pos >= 1:
            self.is_seeking = True
            self.player.set_time(int(seek_pos))
            self.video_moving(None)

        return True  # this will be called inside a timeout so we return True

    def seek_forwards_pressed(self, *args):
        if self.is_seeking or self._timeout_id_backwards != 0 or self._timeout_id_forwards != 0:
            return

        self.is_seeking = True
        timeout = 50

        if self.player.is_playing():
            self.player.pause()
            self.was_playing_before_seek = True
        else:
            self.was_playing_before_seek = False

        self.last_played_rec = None
        self._timeout_id_forwards = GLib.timeout_add(timeout, self.seek_forwards)

    def seek_forwards_released(self, *args):
        # remove timeout
        GLib.source_remove(self._timeout_id_forwards)
        self._timeout_id_forwards = 0

        if self.was_playing_before_seek:
            self.player.play()

        self.is_seeking = False

    def seek_forwards(self):
        seek_pos = self.slider.get_value() + self.seek_step

        if seek_pos < self.video_length_ms:
            self.is_seeking = True
            self.player.set_time(int(seek_pos))
            self.video_moving(None)

        return True  # this will be called inside a timeout so we return True

    def slider_clicked(self, *args):
        self.is_seeking = True

    def slider_released(self, *args):
        slider_pos_ms = int(self.slider.get_value())
        self.player.set_time(slider_pos_ms)
        self.is_seeking = False

    def pause_video(self, *args):
        self.player.pause()
        self.playback_button.set_image(self.play_image)
        self.last_played_rec = None

    def play_video(self, *args):
        self.player.play()
        self.playback_button.set_image(self.pause_image)
        self.last_played_rec = None

    def toggle_player_playback(self, *args):
        if self.player.is_playing():
            self.pause_video(args)
        else:
            self.player.play()
            self.play_video(args)

    def mute_video(self):
        #if not self.player.audio_get_mute():
        self.mute_button.set_image(self.unmute_image)
        self.player.audio_set_mute(True)

    def unmute_video(self):
        #if self.player.audio_get_mute():
        self.mute_button.set_image(self.mute_image)
        self.player.audio_set_mute(False)

    def toggle_audio(self, *args):
        if self.player.audio_get_mute():
            self.unmute_video()
        else:
            self.mute_video()

    def update_time_label(self, ms):
        ms_str = ms_to_timestamp(ms)
        total_length_str = ms_to_timestamp(self.video_length_ms)
        time_txt = ' {} / {} '.format(ms_str, total_length_str)
        self.time_label.set_markup('<span bgcolor="black" fgcolor="white"><tt>{}</tt></span>'.format(time_txt))

    def video_loaded(self, *args):
        # we need to play the video for a while to get the length in milliseconds,
        # so this will be called at the beginning
        self.video_length_ms = self.player.get_length()

        if self.video_length_ms > 0:
            self.slider.set_range(1, self.video_length_ms)
            # self.add_start_end_slider_ticks()
            self.rec_playing_event.set()
            self.pause_video()
            return False  # video has loaded, will not call this again
        else:
            return True  # video not loaded yet, will try again later

    def video_moving(self, *args):
        current_time_ms = self.player.get_time()
        self.slider.set_value(current_time_ms)
        self.update_time_label(current_time_ms)
        self.scroll_annotations_box_to_time(current_time_ms)

        if self.play_recs_with_video and not self.is_seeking and self.highlighed_recording_time is not None:
            rec = self.highlighed_recording_time
            
            if rec and rec != self.last_played_rec:
                self.last_played_rec = rec
                self.rec_queue.put(rec)

    def slider_moved(self, *args):
        # this is called when is moved by the user
        if self.video_length_ms == 0:
            return False  # just to make sure we don't move the slider before we get the video duration

        slider_pos_ms = self.slider.get_value()
        self.player.set_time(int(slider_pos_ms))
        self.update_time_label(slider_pos_ms)
        self.scroll_annotations_box_to_time(slider_pos_ms)

        return False

    def video_ended(self, data):
        GLib.timeout_add(100, self.reload_current_video)  # need to call this with some delay otherwise it gets stuck

    def reload_current_video(self):
        self.player.set_media(self.player.get_media())
        self.slider.set_value(1)
        self.pause_video(None)
        return False  # return False so we stop this timer

    def set_vlc_window(self):
        if sys.platform.startswith('linux'):
            win_id = self.video_area.get_window().get_xid()
            self.player.set_xwindow(win_id)
        elif sys.platform.startswith('darwin'):
            # ugly bit to get window if on mac os
            window = self.video_area.get_property('window')
            ctypes.pythonapi.PyCapsule_GetPointer.restype = ctypes.c_void_p
            ctypes.pythonapi.PyCapsule_GetPointer.argtypes = [ctypes.py_object]
            gpointer = ctypes.pythonapi.PyCapsule_GetPointer(window.__gpointer__, None)
            libgdk = ctypes.CDLL("libgdk-3.dylib")
            libgdk.gdk_quartz_window_get_nsview.restype = ctypes.c_void_p
            libgdk.gdk_quartz_window_get_nsview.argtypes = [ctypes.c_void_p]
            handle = libgdk.gdk_quartz_window_get_nsview(gpointer)
            self.player.set_nsobject(int(handle))
        elif sys.platform.startswith('win'):
            window = self.video_area.get_property('window')
            ctypes.pythonapi.PyCapsule_GetPointer.restype = ctypes.c_void_p
            ctypes.pythonapi.PyCapsule_GetPointer.argtypes = [ctypes.py_object]
            drawingarea_gpointer = ctypes.pythonapi.PyCapsule_GetPointer(window.__gpointer__, None)
            gdkdll = ctypes.CDLL("libgdk-3-0.dll")
            handle = gdkdll.gdk_win32_window_get_handle(drawingarea_gpointer)
            self.player.set_hwnd(int(handle))
        else:
            raise Exception('Cannot deal with this platform: {}'.format(sys.platform))

    def setup_vlc_player(self, widget):
        self.vlc_instance = vlc.Instance('--no-xlib')
        self.player = self.vlc_instance.media_player_new()
        self.rec_player = self.vlc_instance.media_player_new()
        self.set_vlc_window()
        main_events = self.player.event_manager()
        main_events.event_attach(vlc.EventType.MediaPlayerPositionChanged, self.video_moving)
        main_events.event_attach(vlc.EventType.MediaPlayerEndReached, self.video_ended)

        rec_events = self.rec_player.event_manager()
        rec_events.event_attach(vlc.EventType.MediaPlayerEndReached, self.finished_playing_recording)

    def video_area_ready(self, widget):
        self.setup_vlc_player(widget)

    def choose_output_folder(self, default_output):
        dialog = Gtk.FileChooserDialog("Select output folder", self, action=Gtk.FileChooserAction.SELECT_FOLDER,
                                       buttons=(Gtk.STOCK_OK, Gtk.ResponseType.OK))

        dialog.set_current_folder(default_output)
        dialog.run()
        path = dialog.get_filename()
        dialog.destroy()

        return path

    def set_video_recordings_paths_labels(self):
        self.video_path_label.set_text(self.video_path)
        self.recordings_path_label.set_text(self.recordings.video_annotations_folder)

    def setup(self, video_path):
        self.video_path = video_path
        media = self.vlc_instance.media_new_path(self.video_path)
        self.player.set_mrl(media.get_mrl())
        self.playback_button.set_image(self.pause_image)
        self.toggle_media_controls(True)
        self.record_button.set_sensitive(True)
        self.mute_button.set_sensitive(True)
        self.is_video_loaded = True
        self.mute_video()

        video_folder = os.path.dirname(video_path)
        saved_output = self.settings.get_setting('output_path')
        suggested_folder = saved_output if saved_output is not None and os.path.exists(saved_output) else video_folder
        output_path = self.choose_output_folder(suggested_folder)
        self.settings.update_settings(video_folder=video_folder, output_path=output_path)

        if self.recordings is not None:
            # reset things
            self.slider.clear_marks()
            self.remove_all_annotation_boxes()
            self.annotation_box_map = {}
            del self.recordings

        self.recordings = Recordings(output_path, self.video_path)

        GLib.timeout_add(50, self.video_loaded)  # we need to play the video to get the time

        if self.recordings.annotations_exist():
            self.recordings.load_annotations()

            for rec_ms in self.recordings.get_recordings_times():
                self.add_annotation_box(rec_ms)
                self.add_time_tick(rec_ms, colour=self.red_tick_colour)

        self.normal_speed_button.set_active(True)  # reset normal speed
        self.set_video_recordings_paths_labels()
        self.go_to(None, None, 1)
        self.play_video()  # we need to play the video for a while to get the length in milliseconds,


if __name__ == '__main__':
    faulthandler.enable()

    parser = argparse.ArgumentParser()
    parser.add_argument('--query_audio_devices', action='store_true',
                        help='Print the audio devices available in your system')
    parser.add_argument('--set_audio_device', type=int, default=0,
                        help='Set audio device to be used for recording, given the device id. '
                             'Use `--query_audio_devices` to get the devices available in your system with their '
                             'corresponding ids')

    args = parser.parse_args()

    if args.query_audio_devices:
        print(Recorder.get_devices())
        exit()

    narrator = EpicNarrator(mic_device=args.set_audio_device)
    narrator.show()
    Gtk.main()
    narrator.player.stop()
    narrator.vlc_instance.release()
