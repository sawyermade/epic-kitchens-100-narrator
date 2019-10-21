import logging
import os
import traceback

import gi

from player import Player
from recordings import Recordings

gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, GLib, GObject
from recorder import Recorder
from settings import Settings

LOG = logging.getLogger('epic_narrator.recorder')


class SignalSender(GObject.Object):
    @GObject.Signal(flags=GObject.SignalFlags.RUN_FIRST, arg_types=(int, str, str,))
    def video_loaded(self, video_length, video_path, output_path):
        return True

    @GObject.Signal(flags=GObject.SignalFlags.RUN_FIRST, arg_types=(str,))
    def ask_output_path(self, suggested_folder):
        return True

    @GObject.Signal(flags=GObject.SignalFlags.RUN_FIRST, arg_types=(str,))
    def playback_changed(self, state):
        pass

    @GObject.Signal(flags=GObject.SignalFlags.RUN_FIRST, arg_types=(str,))
    def audio_state_changed(self, state):
        pass

    # this is emitted when playing or seeking
    @GObject.Signal(flags=GObject.SignalFlags.RUN_FIRST, arg_types=(int, bool,))
    def video_moving(self, current_position, is_seeking):
        pass

    # this is emitted when jumping to a narration
    @GObject.Signal(flags=GObject.SignalFlags.RUN_FIRST, arg_types=(int,))
    def video_jumped(self, current_position):
        pass

    @GObject.Signal(flags=GObject.SignalFlags.RUN_FIRST, arg_types=(int, int, bool,))
    def recording_added(self, rec_time, rec_idx, new):
        pass

    @GObject.Signal(flags=GObject.SignalFlags.RUN_FIRST, arg_types=(int,))
    def recording_deleted(self, rec_time):
        pass

    @GObject.Signal(flags=GObject.SignalFlags.RUN_FIRST)
    def reset_highlighted_rec(self):
        pass

    @GObject.Signal(flags=GObject.SignalFlags.RUN_FIRST, arg_types=(int, bool,))
    def set_highlighted_rec(self, rec_time_ms, current_recording):
        pass

    @GObject.Signal(flags=GObject.SignalFlags.RUN_FIRST, arg_types=(str,))
    def recording_state_changed(self, state):
        pass

    @GObject.Signal(flags=GObject.SignalFlags.RUN_FIRST, arg_types=(int, bool,))
    def ask_confirmation_for_deleting_rec(self, rec_time_ms, current_rec):
        pass


class Controller:
    def __init__(self):
        self.settings = Settings()
        self.recorder = self.create_recorder()
        self.recordings = None
        self.video_length = 0
        self.is_video_loaded = False
        self.video_path = None  # TODO load things from settings
        self.output_path = None
        self.player = None
        self.signal_sender = SignalSender()
        self.signal_sender.connect('video_moving', self.find_closest_rec)
        self.holding_enter = False
        self.was_playing_before_recording = False
        self.stop_recording_delay_ms = 500
        self.is_dragging = False
        self.highlighted_rec = None

    def create_recorder(self):
        saved_microphone = self.settings.get_setting('microphone')

        if saved_microphone is not None:
            try:
                recorder = Recorder(device_id=saved_microphone)
            except Exception:
                recorder = Recorder()
                default_mic_device = Recorder.get_default_device()

                LOG.error('Could not use device with ID {}. This is likely due to a saved configuration '
                          'that is no longer available '
                          '(e.g. you used a device that is not plugged anymore).'
                          'Using default mic with ID {} now'.format(saved_microphone, default_mic_device))
                self.settings.update_settings(microphone=default_mic_device)
        else:
            recorder = Recorder()

        recorder.stream.start()

        return recorder

    def get_mic_devices(self):
        return Recorder.get_devices()

    def get_current_mic_device(self):
        return self.recorder.device_id

    def get_setting(self, key, default_value):
        setting = self.settings.get_setting(key)
        return setting if setting is not None else default_value

    def get_video_length(self):
        return self.video_length

    def set_video_length(self, video_length):
        self.video_length = video_length

    def get_recorder_data(self):
        return self.recorder.q.get_nowait()

    def is_recording(self):
        return self.recorder.is_recording

    def shutting_down(self, *args):
        '''
        if self.is_video_loaded:
            self.settings.update_settings(last_video_position=self.player.get_time())
        '''

        self.recorder.close_stream()

        if self.is_video_loaded:
            self.settings.update_settings(last_video_position=self.player.get_current_position())

        if self.player is not None:
            self.player.shutting_down()

        Gtk.main_quit()

    def change_mic(self, mic_id):
        if self.recorder.is_recording:
            self.recorder.stop_recording()

        try:
            self.recorder.change_device(mic_id)
            self.recorder.stream.start()  # starts the microphone stream
            self.settings.update_settings(microphone=mic_id)
            return True
        except Exception:
            LOG.error(traceback.format_exc())
            return False

    def get_recorder_window_size(self):
        return self.recorder.get_window_size()

    def is_output_path_valid(self, output_path, video_path):
        return not(output_path is None or
                   not os.path.exists(Recordings.get_recordings_path_for_video(output_path, video_path)))

    def video_selected(self, video_path):
        self.video_path = video_path
        self.settings.update_settings(last_video=video_path)
        video_folder = os.path.dirname(video_path)
        self.settings.update_settings(video_folder=video_folder)

        saved_output = self.get_setting('output_path', None)

        if not self.is_output_path_valid(saved_output, self.video_path):
            suggested_folder = saved_output if saved_output is not None and \
                                               os.path.exists(saved_output) \
                                               else video_folder
            self.signal_sender.emit('ask_output_path', suggested_folder)
        else:
            self.output_folder_path_selected(saved_output)

    def output_folder_path_selected(self, output_path):
        self.output_path = output_path
        self.settings.update_settings(output_path=output_path)
        self.setup_narrator(self.video_path, output_path)

    def ui_video_area_ready(self, widget):
        self.player = Player(widget, self)

        last_video_path = self.get_setting('last_video', None)

        if last_video_path is not None and os.path.exists(last_video_path):
            self.video_selected(last_video_path)

    def setup_narrator(self, video_path, output_path):
        # TODO check this and implement reloading of video
        self.video_path = video_path
        self.output_path = output_path
        self.is_video_loaded = False
        video_folder = os.path.dirname(video_path)
        self.settings.update_settings(last_video=video_path)
        self.settings.update_settings(video_folder=video_folder, output_path=self.output_path)

        playback_speed = self.get_setting('playback_speed', 1)

        if type(playback_speed) == float and 0 <= playback_speed <= 1:
            self.player.set_speed(playback_speed)

        if self.recordings is not None:
            # TODO reset things
            pass

        self.recordings = Recordings(self.output_path, self.video_path)
        self.player.load_video(video_path)

        if self.recordings.narrations_exist():
            self.recordings.load_narrations()

            for rec_idx, rec_ms in enumerate(self.recordings.get_recordings_times()):
                self.signal_sender.emit('recording_added', rec_ms, rec_idx, False)

    def video_loaded(self):
        self.is_video_loaded = True
        self.video_length = self.player.get_video_length()
        self.signal_sender.emit('video_loaded', self.video_length, self.video_path,
                                self.recordings.video_narrations_folder)

        last_position = self.get_setting('last_video_position', 1)
        self.go_to(last_position)

    def playback_speed_selected(self, sender, speed):
        self.settings.update_settings(playback_speed=speed)

        if self.is_video_loaded:
            self.player.set_speed(speed)

    def hold_to_record_toggled(self, widget):
        self.settings.update_settings(hold_to_record=widget.get_active())

    def play_after_delete_toggled(self, widget):
        self.settings.update_settings(play_after_delete=widget.get_active())

    def play_video(self, *args):
        if not self.is_video_loaded or self.recorder.is_recording:
            return

        LOG.info("Play video")
        self.signal_sender.emit('playback_changed', 'play')
        self.player.play_video()
        # print('play', threading.current_thread())

    def pause_video(self, *args):
        if not self.is_video_loaded or self.recorder.is_recording:
            return

        LOG.info("Pause video")
        self.signal_sender.emit('playback_changed', 'pause')
        self.player.pause_video()
        # print('pause', threading.current_thread())

    def toggle_player_playback(self, *args):
        if not self.is_video_loaded or self.recorder.is_recording:
            return

        LOG.info("Toggle playback")
        if self.player.is_playing():
            self.pause_video()
        else:
            self.play_video()

    def toggle_audio(self, *args):
        if not self.is_video_loaded or self.recorder.is_recording:
            return

        LOG.info("Toggle audio")
        if self.player.is_mute():
            self.unmute_video()
        else:
            self.mute_video()

    def mute_video(self):
        LOG.info("Mute video")
        self.player.mute_video()
        self.signal_sender.emit('audio_state_changed', 'muted')

    def unmute_video(self):
        LOG.info("Unmute video")
        self.player.unmute_video()
        self.signal_sender.emit('audio_state_changed', 'unmuted')

    def reset_highlighted_rec(self):
        self.signal_sender.emit('reset_highlighted_rec')
        self.recordings.reset_highlighted()
        self.highlighted_rec = None

    def start_seek(self, widget, direction):
        if not self.is_video_loaded or self.player.is_seeking() or self.recorder.is_recording:
            return

        self.reset_highlighted_rec()
        self.player.start_seek(direction)

        if self.player.was_playing_before_seek:
            self.signal_sender.emit('playback_changed', 'pause')

    def stop_seek(self, *args):
        if not self.is_video_loaded or not self.player.is_seeking():
            return

        self.player.stop_seek()

        if self.player.was_playing_before_seek:
            self.signal_sender.emit('playback_changed', 'play')

    def go_to(self, time_ms, jumped=False):
        if not self.is_video_loaded or self.recorder.is_recording:
            return

        if time_ms < 0 or time_ms > self.video_length:
            return

        self.recordings.reset_highlighted()
        self.player.go_to(int(time_ms))

        if jumped:
            # this will not highlight
            self.signal_sender.emit('video_jumped', self.player.get_current_position())
        else:
            # the controller is connected to this signal, so it will highlight and scroll to the narration
            self.signal_sender.emit('video_moving', self.player.get_current_position(), self.player.is_seeking())

    def start_dragging(self):
        if not self.is_video_loaded or self.recorder.is_recording:
            return

        self.is_dragging = True
        self.reset_highlighted_rec()

        if self.player.is_playing():
            self.pause_video()
            self.player.was_playing_before_seek = True

    def stop_dragging(self):
        self.is_dragging = False

        if self.player.was_playing_before_seek:
            self.play_video()

    def find_closest_rec(self, sender, time_ms, is_seeking):
        if is_seeking or self.is_dragging:
            rec = self.recordings.get_closest_recording(time_ms)

            if rec is not None:
                self.highlighted_rec = rec
                self.signal_sender.emit('set_highlighted_rec', self.highlighted_rec, False)
            else:
                self.reset_highlighted_rec()
        else:
            rec = self.recordings.get_next_from_highlighted(time_ms)

            if rec is not None:
                self.highlighted_rec = rec
                self.recordings.move_highlighted_next()
                self.signal_sender.emit('set_highlighted_rec', self.highlighted_rec, False)

    def record_button_clicked(self, *args):
        LOG.info("Record button pressed")
        if self.get_setting('hold_to_record', False):
            if not self.holding_enter and not self.recorder.is_recording:
                self.holding_enter = True
                self.start_recording()
        else:
            self.toggle_record()

    def record_button_released(self, *args):
        LOG.info("Record button released")
        if self.get_setting('hold_to_record', False):
            if self.recorder.is_recording:
                self.invoke_stop_recording()

    def toggle_record(self):
        LOG.info("Toggle recording")

        if not self.recorder.is_recording:
            self.start_recording()
        else:
            self.invoke_stop_recording()

    def start_recording(self, overwrite=False, rec_time=None):
        # first start the recording and then update the ui to prevent clipping
        if self.player.is_playing():
            self.pause_video()
            self.was_playing_before_recording = True
        else:
            self.was_playing_before_recording = False

        if overwrite:
            if rec_time is None or not self.recordings.recording_exists(rec_time):
                return

            path, _ = self.recordings.add_recording(rec_time, overwrite=overwrite)
            rec_idx = None
        else:
            rec_time = self.player.get_current_position()

            if rec_time < 0:  # happens when we reach the end
                return

            while self.recordings.recording_exists(rec_time):
                rec_time += 1  # shifting one millisecond

            path, rec_idx = self.recordings.add_recording(rec_time, overwrite=overwrite)

        self.recorder.start_recording(path)
        self.highlighted_rec = rec_time

        if not overwrite:
            self.signal_sender.emit('recording_added', rec_time, rec_idx, True)

        self.signal_sender.emit('recording_state_changed', 'recording')

    def invoke_stop_recording(self):
        LOG.info("Stop recording in 0.5 seconds")
        GLib.timeout_add(self.stop_recording_delay_ms, self.stop_recording)  # TODO resume playing

    def stop_recording(self):
        self.recorder.stop_recording()

        LOG.info("Recording stopped")
        self.signal_sender.emit('recording_state_changed', 'not_recording')
        self.reset_highlighted_rec()
        self.holding_enter = False

        if self.was_playing_before_recording:
            self.play_video()

        return False  # reset the GLib timer

    def play_recording(self, time_ms):
        LOG.info("Playing recording at {}ms".format(time_ms))
        recording_path = self.recordings.get_path_for_recording(time_ms)

        if recording_path is not None:
            self.player.play_recording(recording_path)

    def delete_recording(self, time_ms):
        if self.player.is_playing():
            self.pause_video()

        if time_ms == self.highlighted_rec:
            self.reset_highlighted_rec()

        if self.recorder.is_recording:
            self.stop_recording()

        self.recordings.delete_recording(time_ms)
        self.signal_sender.emit('recording_deleted', time_ms)

        if self.get_setting('play_after_delete', False):
            self.play_video()

    def get_recording_times(self):
        return self.recordings.get_recordings_times()

    def main_window_key_pressed(self, widget, event):
        if not self.is_video_loaded:
            return True

        if event.keyval == Gdk.KEY_Left:
            self.start_seek(None, 'backward')
        elif event.keyval == Gdk.KEY_Right:
            self.start_seek(None, 'forward')
        elif event.keyval == Gdk.KEY_space:
            self.toggle_player_playback()
        elif event.keyval == Gdk.KEY_Return:
            if self.holding_enter:
                return True

            # We can't rely only on the recorder variable to avoid ghosting recordings, because this code can be called
            # multiple times before the recording starts. We use a simple variable here
            # this will be set to False when actually finishing the recording
            self.holding_enter = True
            LOG.info("Pressing enter")

            if self.get_setting('hold_to_record', False):
                if not self.recorder.is_recording:
                    self.start_recording()
        else:
            pass

        # return true does not propagate the event to other widgets
        return True

    def main_window_key_released(self, widget, event):
        if not self.is_video_loaded:
            return True

        if event.keyval == Gdk.KEY_Left or event.keyval == Gdk.KEY_Right:
            self.stop_seek()
        elif event.keyval == Gdk.KEY_Return:
            LOG.info("Enter released")

            if self.get_setting('hold_to_record', False):
                if self.recorder.is_recording:
                    self.invoke_stop_recording()
            else:
                self.toggle_record()
        elif event.keyval == Gdk.KEY_o or event.keyval == Gdk.KEY_O:
            pass # fix this
            '''
            if self.highlighed_recording_time is not None:
                self.overwrite_recording(self.highlighed_recording_time)
            '''
        elif event.keyval == Gdk.KEY_M or event.keyval == Gdk.KEY_m:
            self.toggle_audio()
        elif event.keyval == Gdk.KEY_Delete or event.keyval == Gdk.KEY_BackSpace:
            if self.recordings.empty() or self.highlighted_rec is None:
                return True

            self.pause_video()
            current_recording = self.is_recording()
            self.signal_sender.emit('ask_confirmation_for_deleting_rec', self.highlighted_rec, current_recording)
        else:
            pass

        # return true does not propagate the event to other widgets
        return True
