<template lang="pug">
div
  div.d-flex.align-items-center.mb-3
    h3.mb-0 AutoCAB Sessions
    button.btn.btn-link.p-0.ml-2.text-muted(
      id="sessions-help"
      type="button"
      aria-label="About sessions"
    )
      icon(name="question-circle")
    b-popover(
      target="sessions-help"
      triggers="hover focus click blur"
      placement="bottom"
      title="About sessions"
    )
      | Start a named session to capture screenshots+OCR for a task. Stop it when
      | you're done. Starting again with the same task name resumes that task
      | across multiple sessions, so they can later be merged for skill-drafting.

  div.alert.alert-warning(v-if="backendError")
    | Can't reach the session backend at #[code http://localhost:5677] —
    | is it running? (#[code cd activitywatch/backend && python3 session_controller.py])

  b-input-group(size="lg")
    b-input(
      v-model="taskName"
      placeholder="Task name, e.g. 'fixing-terminal-log-bug'"
      aria-label="Task name"
      @keyup.enter="startSession(taskName)"
    )
    b-input-group-append
      b-button(@click="startSession(taskName)", variant="success", :disabled="!taskName.trim()")
        icon(name="play")
        | Start

  hr

  div(v-if="loading")
    b-spinner.mr-2(small)
    span.text-muted Loading...
  div(v-else)
    div(v-if="tasks.length === 0")
      p.text-muted.mb-0 No sessions yet. Start one above to begin capturing a task.

    div(v-for="task in tasks", :key="task.task_name", class="mb-4")
      h5.mt-3.mb-2
        | {{ task.task_name }}
        span.text-muted.ml-2(style="font-size: 0.8em")
          | {{ task.session_count }} session{{ task.session_count === 1 ? '' : 's' }}
        b-button.ml-2(size="sm", variant="outline-success", @click="startSession(task.task_name)")
          icon(name="play")
          | Resume

      b-table-simple(small, borderless)
        b-tbody
          b-tr(v-for="s in task.sessions", :key="s.session_id")
            b-td
              b-badge(:variant="s.status === 'running' ? 'success' : 'secondary'")
                | {{ s.status }}
            b-td.text-muted {{ formatTime(s.start_time) }}
            b-td.text-muted(v-if="s.stop_time") → {{ formatTime(s.stop_time) }}
            b-td(v-else)
            b-td
              b-button(v-if="s.status === 'running'", size="sm", variant="outline-danger", @click="stopSession(s.session_id)")
                icon(name="stop")
                | Stop
</template>

<style scoped lang="scss">
.btn {
  margin-right: 0.5em;

  .fa-icon {
    margin-left: 0;
    margin-right: 0.5em;
  }
}
</style>

<script lang="ts">
import moment from 'moment';
import 'vue-awesome/icons/play';
import 'vue-awesome/icons/stop';
import 'vue-awesome/icons/question-circle';

const BACKEND_URL = 'http://localhost:5677';

export default {
  name: 'Sessions',
  data: () => {
    return {
      loading: true,
      backendError: false,
      taskName: '',
      tasks: [] as Array<{ task_name: string; session_count: number; sessions: any[] }>,
      poller: null as ReturnType<typeof setInterval> | null,
    };
  },
  mounted: async function () {
    await this.refresh();
    this.poller = setInterval(() => this.refresh(), 3000);
  },
  beforeDestroy: function () {
    if (this.poller) clearInterval(this.poller);
  },
  methods: {
    formatTime(iso: string) {
      return moment(iso).format('HH:mm:ss');
    },
    refresh: async function () {
      try {
        const res = await fetch(`${BACKEND_URL}/api/tasks`);
        this.tasks = await res.json();
        this.backendError = false;
      } catch (e) {
        this.backendError = true;
      }
      this.loading = false;
    },
    startSession: async function (task_name: string) {
      if (!task_name || !task_name.trim()) return;
      try {
        await fetch(`${BACKEND_URL}/api/session/start`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ task_name: task_name.trim() }),
        });
        this.taskName = '';
        await this.refresh();
      } catch (e) {
        this.backendError = true;
      }
    },
    stopSession: async function (session_id: string) {
      try {
        await fetch(`${BACKEND_URL}/api/session/stop`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ session_id }),
        });
        await this.refresh();
      } catch (e) {
        this.backendError = true;
      }
    },
  },
};
</script>
