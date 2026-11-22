<script setup lang="ts">
import { computed } from 'vue';
import type { ScheduleItem, ScheduleResponse } from '../api/timetable';
import AppIcon from './AppIcon.vue';

const props = defineProps<{
  items: ScheduleItem[];
  deadlines: ScheduleResponse['deadlines'];
  anchorDate: string;
  view: 'day' | 'week' | 'month' | 'year';
}>();

const emit = defineEmits<{
  selectDay: [date: string];
  selectItem: [item: ScheduleItem];
}>();

type PositionedItem = {
  item: ScheduleItem;
  start: number;
  end: number;
  lane: number;
  lanes: number;
};

const weekdays = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];
const tonePalette = [
  { background: '#dce8ff', border: '#5d84c4', text: '#18345e' },
  { background: '#e3e0fb', border: '#8069bd', text: '#3c2a6b' },
  { background: '#dff1e9', border: '#4b937d', text: '#194b3d' },
  { background: '#ffe9d9', border: '#c47a46', text: '#613619' },
  { background: '#f9dfe8', border: '#be6c89', text: '#64283f' },
  { background: '#f4edcc', border: '#b08c31', text: '#5b4612' },
];

const parsedAnchor = computed(() => parseDate(props.anchorDate));
// 年视图展示实际日期与事务密度，点击日期进入现有日视图。
const yearMonths = computed(() => Array.from({ length: 12 }, (_, month) => {
  const year = parsedAnchor.value.getFullYear();
  const first = new Date(year, month, 1, 12);
  return { month: month + 1, offset: (first.getDay() + 6) % 7,
    dates: Array.from({ length: new Date(year, month + 1, 0, 12).getDate() }, (_, day) => toDateString(new Date(year, month, day + 1, 12))) };
}));
const itemsByDate = computed(() => {
  const result = new Map<string, ScheduleItem[]>();
  for (const item of props.items) {
    const list = result.get(item.local_date) ?? [];
    list.push(item);
    result.set(item.local_date, list);
  }
  return result;
});
const deadlinesByDate = computed(() => {
  const result = new Map<string, ScheduleResponse['deadlines']>();
  for (const deadline of props.deadlines) {
    const list = result.get(deadline.local_date) ?? [];
    list.push(deadline);
    result.set(deadline.local_date, list);
  }
  return result;
});

const visibleDates = computed(() => {
  if (props.view === 'day') return [props.anchorDate];
  if (props.view === 'week') {
    const monday = startOfWeek(parsedAnchor.value);
    return Array.from({ length: 7 }, (_, index) => toDateString(addDays(monday, index)));
  }
  const gridStart = startOfWeek(new Date(parsedAnchor.value.getFullYear(), parsedAnchor.value.getMonth(), 1, 12));
  return Array.from({ length: 42 }, (_, index) => toDateString(addDays(gridStart, index)));
});

const allTimedItems = computed(() => visibleDates.value.flatMap((date) => (
  (itemsByDate.value.get(date) ?? []).filter((item) => item.start_at)
)));
// 日视图只呈现实际安排，稀疏课表也无需滚过整天的空白刻度。
const dailyAgenda = computed(() => [...allTimedItems.value]
  .sort((left, right) => timeMinutes(left.start_at) - timeMinutes(right.start_at)));
// 日视图可直接点选本周日期；周、月视图仍使用各自的完整网格。
const agendaWeek = computed(() => {
  const monday = startOfWeek(parsedAnchor.value);
  return Array.from({ length: 7 }, (_, index) => toDateString(addDays(monday, index)));
});
const timeBounds = computed(() => {
  const starts = allTimedItems.value.map((item) => timeMinutes(item.start_at));
  const ends = allTimedItems.value.map((item) => timeMinutes(item.end_at ?? item.start_at));
  const min = Math.min(8 * 60, ...starts.filter(Number.isFinite));
  const max = Math.max(22 * 60, ...ends.filter(Number.isFinite));
  return {
    start: Math.max(0, Math.floor(min / 60) * 60),
    end: Math.min(24 * 60, Math.ceil(max / 60) * 60),
  };
});
const hourMarks = computed(() => {
  const marks: number[] = [];
  for (let minute = timeBounds.value.start; minute <= timeBounds.value.end; minute += 60) marks.push(minute);
  return marks;
});
const timedItemsByDate = computed(() => {
  const result = new Map<string, PositionedItem[]>();
  for (const date of visibleDates.value) {
    result.set(date, layoutDay((itemsByDate.value.get(date) ?? []).filter((item) => item.start_at)));
  }
  return result;
});
const allDayEntries = computed(() => visibleDates.value.flatMap((date) => {
  const items = (itemsByDate.value.get(date) ?? [])
    .filter((item) => !item.start_at)
    .map((item) => ({ id: item.id, date, title: item.title, kind: item.kind ?? 'all_day', item }));
  const deadlines = (deadlinesByDate.value.get(date) ?? [])
    .map((deadline) => ({ id: deadline.id, date, title: deadline.title, kind: 'deadline' as const, item: null }));
  return [...items, ...deadlines];
}));
const today = toDateString(new Date());

function parseDate(value: string): Date {
  return new Date(`${value}T12:00:00`);
}

function toDateString(value: Date): string {
  const year = value.getFullYear();
  const month = String(value.getMonth() + 1).padStart(2, '0');
  const day = String(value.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

function addDays(value: Date, days: number): Date {
  const date = new Date(value);
  date.setDate(date.getDate() + days);
  return date;
}

function startOfWeek(value: Date): Date {
  const date = new Date(value);
  const daysFromMonday = (date.getDay() + 6) % 7;
  date.setDate(date.getDate() - daysFromMonday);
  return date;
}

function timeMinutes(value: string | null): number {
  if (!value) return Number.NaN;
  const clock = value.match(/(\d{2}):(\d{2})/);
  if (!clock) return Number.NaN;
  return Number(clock[1]) * 60 + Number(clock[2]);
}

function formatClock(value: string | null): string {
  if (!value) return '';
  const clock = value.match(/(\d{2}):(\d{2})/);
  return clock ? `${clock[1]}:${clock[2]}` : '';
}

function formatHour(minute: number): string {
  return `${String(Math.floor(minute / 60)).padStart(2, '0')}:00`;
}

function formatMonthDay(value: string): string {
  return `${Number(value.slice(5, 7))}/${Number(value.slice(8, 10))}`;
}

function weekdayLabel(value: string): string {
  return weekdays[(parseDate(value).getDay() + 6) % 7];
}

function isToday(value: string): boolean {
  return value === today;
}

function isCurrentMonth(value: string): boolean {
  return value.slice(0, 7) === props.anchorDate.slice(0, 7);
}

function layoutDay(items: ScheduleItem[]): PositionedItem[] {
  const sorted = [...items].sort((left, right) => timeMinutes(left.start_at) - timeMinutes(right.start_at));
  const clusters: ScheduleItem[][] = [];
  let cluster: ScheduleItem[] = [];
  let clusterEnd = -1;
  for (const item of sorted) {
    const start = timeMinutes(item.start_at);
    if (cluster.length && start >= clusterEnd) {
      clusters.push(cluster);
      cluster = [];
    }
    cluster.push(item);
    clusterEnd = Math.max(clusterEnd, timeMinutes(item.end_at ?? item.start_at));
  }
  if (cluster.length) clusters.push(cluster);

  const positioned: PositionedItem[] = [];
  for (const group of clusters) {
    const laneEnds: number[] = [];
    const assigned = group.map((item) => {
      const start = timeMinutes(item.start_at);
      const end = Math.max(start + 30, timeMinutes(item.end_at ?? item.start_at));
      let lane = laneEnds.findIndex((laneEnd) => laneEnd <= start);
      if (lane < 0) lane = laneEnds.length;
      laneEnds[lane] = end;
      return { item, start, end, lane, lanes: 1 };
    });
    for (const item of assigned) item.lanes = laneEnds.length;
    positioned.push(...assigned);
  }
  return positioned;
}

function eventStyle(position: PositionedItem): Record<string, string> {
  const { item, start, end, lane, lanes } = position;
  const tone = toneFor(item.title);
  const total = timeBounds.value.end - timeBounds.value.start;
  return {
    '--event-top': `${((start - timeBounds.value.start) / total) * 100}%`,
    '--event-height': `${Math.max(2.5, ((end - start) / total) * 100)}%`,
    '--event-left': `${(lane / lanes) * 100}%`,
    '--event-width': `${100 / lanes}%`,
    '--event-bg': tone.background,
    '--event-border': tone.border,
    '--event-text': tone.text,
  };
}

function hourStyle(minute: number): Record<string, string> {
  const total = timeBounds.value.end - timeBounds.value.start;
  return { '--hour-top': `${((minute - timeBounds.value.start) / total) * 100}%` };
}

function toneFor(title: string) {
  let hash = 0;
  for (const character of title) hash = (hash * 31 + character.charCodeAt(0)) >>> 0;
  return tonePalette[hash % tonePalette.length];
}

function eventAriaLabel(item: ScheduleItem): string {
  return `${item.title}，${formatClock(item.start_at)}至${formatClock(item.end_at)}${item.room ? `，${item.room}` : ''}`;
}
</script>

<template>
  <div class="schedule-calendar" :data-view="view">
    <div v-if="view === 'year'" class="year-grid" aria-label="全年日程">
      <section v-for="month in yearMonths" :key="month.month" class="year-month">
        <h2>{{ month.month }} 月</h2>
        <div class="year-days"><span v-for="day in weekdays" :key="day" class="year-weekday">{{ day.slice(1) }}</span>
          <span v-for="blank in month.offset" :key="`blank-${blank}`" aria-hidden="true"></span>
          <button v-for="date in month.dates" :key="date" type="button" :class="{ today: isToday(date), occupied: itemsByDate.has(date), due: deadlinesByDate.has(date) }"
            :aria-label="`${date}，${(itemsByDate.get(date) ?? []).length} 项安排，${(deadlinesByDate.get(date) ?? []).length} 个截止`" @click="emit('selectDay', date)">
            {{ Number(date.slice(8, 10)) }}</button>
        </div>
      </section>
      <p>底色表示有安排，圆点表示有截止事项。选择日期查看详情。</p>
    </div>
    <template v-else-if="view === 'month'">
      <div class="month-weekdays" aria-hidden="true">
        <span v-for="day in weekdays" :key="day">{{ day }}</span>
      </div>
      <div class="month-grid">
        <button
          v-for="date in visibleDates"
          :key="date"
          type="button"
          class="month-day"
          :class="{ outside: !isCurrentMonth(date), today: isToday(date) }"
          :aria-label="`${date}，${(itemsByDate.get(date) ?? []).length} 项安排`"
          @click="emit('selectDay', date)"
        >
          <span class="month-day-number">{{ Number(date.slice(8, 10)) }}</span>
          <span v-for="item in (itemsByDate.get(date) ?? []).slice(0, 2)" :key="item.id" class="month-event" :style="{ '--month-event-color': toneFor(item.title).border }">
            <i aria-hidden="true"></i>{{ formatClock(item.start_at) }} {{ item.title }}
          </span>
          <small v-if="(itemsByDate.get(date) ?? []).length > 2">+{{ (itemsByDate.get(date) ?? []).length - 2 }} 项</small>
          <small v-if="(deadlinesByDate.get(date) ?? []).length" class="month-deadline">{{ (deadlinesByDate.get(date) ?? []).length }} 个截止</small>
        </button>
      </div>
    </template>

    <template v-else>
      <nav v-if="view === 'day'" class="calendar-week-strip" aria-label="选择本周日期">
        <button
          v-for="date in agendaWeek"
          :key="date"
          type="button"
          :aria-label="`${date} ${weekdayLabel(date)}`"
          :aria-current="date === anchorDate ? 'date' : undefined"
          @click="emit('selectDay', date)"
        >
          <span>{{ weekdayLabel(date) }}</span>
          <strong>{{ Number(date.slice(8)) }}</strong>
        </button>
      </nav>
      <div v-if="allDayEntries.length" class="calendar-all-day">
        <strong>全天与截止</strong>
        <div>
          <button
            v-for="entry in allDayEntries"
            :key="`${entry.kind}-${entry.id}`"
            type="button"
            :class="{ deadline: entry.kind === 'deadline' }"
            @click="entry.item ? emit('selectItem', entry.item) : emit('selectDay', entry.date)"
          >
            <span>{{ formatMonthDay(entry.date) }}</span>{{ entry.title }}
          </button>
        </div>
      </div>

      <section v-if="view === 'day'" class="calendar-agenda" aria-label="当日安排">
        <header class="calendar-agenda-header">
          <h2>{{ isToday(anchorDate) ? '今天' : '当日安排' }}</h2>
          <span>{{ dailyAgenda.length }} 项定时安排</span>
        </header>
        <ol v-if="dailyAgenda.length" class="calendar-agenda-list">
          <li v-for="item in dailyAgenda" :key="item.id">
            <button type="button" class="calendar-agenda-event" @click="emit('selectItem', item)">
              <span class="calendar-agenda-icon" :data-course="item.item_type === 'course'">
                <AppIcon :name="item.item_type === 'course' ? 'calendar_month' : 'event'" />
              </span>
              <span class="calendar-agenda-copy">
                <strong>{{ item.title }}</strong>
                <span class="calendar-agenda-time">{{ formatClock(item.start_at) }}<template v-if="item.end_at">–{{ formatClock(item.end_at) }}</template></span>
                <span class="calendar-agenda-meta">{{ item.item_type === 'course' ? '课程' : '个人安排' }}<template v-if="item.room"> · {{ item.room }}</template></span>
              </span>
              <AppIcon name="chevron_right" class="calendar-agenda-arrow" />
            </button>
          </li>
        </ol>
        <p v-else class="calendar-agenda-empty">当天没有定时安排。</p>
      </section>

      <div v-else class="calendar-grid-scroll">
        <div
          class="calendar-grid"
          :class="{ 'calendar-grid--week': view === 'week' }"
          :style="{ '--calendar-columns': visibleDates.length }"
        >
          <div class="calendar-corner" aria-hidden="true"></div>
          <button
            v-for="date in visibleDates"
            :key="`heading-${date}`"
            type="button"
            class="calendar-day-heading"
            :class="{ today: isToday(date) }"
            @click="emit('selectDay', date)"
          >
            <span>{{ weekdayLabel(date) }}</span>
            <strong>{{ formatMonthDay(date) }}</strong>
          </button>

          <div class="calendar-time-axis" :style="{ '--calendar-total': timeBounds.end - timeBounds.start }">
            <span v-for="minute in hourMarks" :key="minute" class="calendar-hour-label" :style="hourStyle(minute)">
              {{ formatHour(minute) }}
            </span>
          </div>

          <div v-for="date in visibleDates" :key="`column-${date}`" class="calendar-day-column">
            <i v-for="minute in hourMarks" :key="minute" class="calendar-hour-line" :style="hourStyle(minute)" aria-hidden="true"></i>
            <button
              v-for="position in (timedItemsByDate.get(date) ?? [])"
              :key="position.item.id"
              type="button"
              class="calendar-event"
              :style="eventStyle(position)"
              :aria-label="eventAriaLabel(position.item)"
              @click="emit('selectItem', position.item)"
            >
              <strong>{{ position.item.title }}</strong>
              <span>{{ formatClock(position.item.start_at) }}–{{ formatClock(position.item.end_at) }}</span>
              <small v-if="position.item.room">{{ position.item.room }}</small>
            </button>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
/* 小屏按单列展示月份；保留足够的日期点击范围。 */
.year-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:20px;padding:16px;overflow:auto;width:100%;box-sizing:border-box}
.year-grid>p{grid-column:1/-1}.year-month h2{font-size:1rem;margin:0 0 12px}.year-days{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:4px}
.year-days button{position:relative;border:0;border-radius:8px;min-height:32px;background:transparent;color:inherit;cursor:pointer;font:inherit}
.year-days .occupied{background:var(--app-primary-container,#e7edf8)}.year-days .today{outline:2px solid var(--app-primary,#42699f)}.year-days .due:after{content:'';position:absolute;bottom:2px;left:calc(50% - 2px);width:4px;height:4px;background:#a34242;border-radius:50%}.year-weekday{text-align:center;opacity:.7;font-size:12px}
/* 选中日期用主色容器区分，其他日期保持同样的点击范围。 */
.calendar-week-strip {
  display: grid;
  flex: 0 0 auto;
  grid-template-columns: repeat(7, minmax(0, 1fr));
  gap: 2px;
  padding: 10px;
  border-bottom: 1px solid var(--app-outline);
}
.calendar-week-strip button {
  display: grid;
  min-width: 0;
  min-height: 56px;
  place-content: center;
  gap: 3px;
  border: 0;
  border-radius: 18px;
  background: transparent;
  color: var(--app-on-surface-variant);
  cursor: pointer;
}
.calendar-week-strip button[aria-current='date'] {
  background: rgb(var(--mdui-color-primary-container));
  color: rgb(var(--mdui-color-on-primary-container));
}
.calendar-week-strip button:hover { background: rgb(var(--mdui-color-secondary-container)); }
.calendar-week-strip button:focus-visible { outline: 2px solid var(--app-primary); outline-offset: -2px; }
.calendar-week-strip span { font-size: 11px; }
.calendar-week-strip strong { font-size: 17px; font-variant-numeric: tabular-nums; }

/* 日列表两侧直接对齐日期条，课程行只保留自身的内边距。 */
.calendar-agenda {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 0;
  background: var(--app-surface);
}

.calendar-agenda-header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}

.calendar-agenda-header h2 { margin: 0; font-size: 17px; }
.calendar-agenda-header span,
.calendar-agenda-meta,
.calendar-agenda-empty { color: var(--app-on-surface-variant); font-size: 13px; }

.calendar-agenda-list {
  display: grid;
  gap: 8px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.calendar-agenda-event {
  display: grid;
  width: 100%;
  grid-template-columns: 40px minmax(0, 1fr) 20px;
  align-items: center;
  gap: 12px;
  padding: 12px;
  color: var(--app-on-surface);
  border: 1px solid var(--app-outline);
  border-radius: 14px;
  background: var(--app-surface-container);
  text-align: left;
  cursor: pointer;
}

.calendar-agenda-event:hover { border-color: var(--app-primary); }
.calendar-agenda-event:focus-visible { outline: 2px solid var(--app-primary); outline-offset: 2px; }
.calendar-agenda-icon {
  display: grid;
  height: 40px;
  place-items: center;
  border-radius: 12px;
  color: var(--app-on-surface-variant);
  background: var(--app-surface);
}
.calendar-agenda-icon[data-course='true'] { color: var(--app-primary); }
.calendar-agenda-copy { display: grid; gap: 3px; min-width: 0; overflow-wrap: anywhere; }
.calendar-agenda-copy strong { font-size: 16px; line-height: 1.4; }
.calendar-agenda-time { color: var(--app-primary); font-size: 14px; font-variant-numeric: tabular-nums; }
.calendar-agenda-arrow { width: 20px; height: 20px; color: var(--app-on-surface-variant); }
</style>
