(function(){
  'use strict';

  const SVG_NAMESPACE = 'http://www.w3.org/2000/svg';
  const TIMELINE_BUCKET_COUNT = 24;
  const CATEGORIES = [
    {
      key:'screen',
      label:'Screen',
      types:new Set([
        'screen.frame', 'screen.ocr', 'screen.window',
        'screen.recording.started', 'screen.recording.stopped'
      ]),
      color:'var(--chart-1)'
    },
    {
      key:'agents',
      label:'Agents',
      types:new Set(['agent.message', 'agent.adapter.failed']),
      color:'var(--chart-2)'
    },
    {
      key:'shell',
      label:'Shell',
      types:new Set(['shell.command.completed']),
      color:'var(--chart-3)'
    },
    {
      key:'files-git',
      label:'Files & Git',
      types:new Set(['file.changed', 'file.diff', 'file.flood', 'git.snapshot']),
      color:'var(--chart-4)'
    },
    {
      key:'jobs',
      label:'Jobs',
      types:new Set(['job.submitted', 'job.completed']),
      color:'var(--chart-5)'
    },
    {
      key:'notes-session',
      label:'Notes & Session',
      types:new Set(),
      color:'var(--chart-6)'
    }
  ];

  function requireElement(root, selector){
    const element = root.querySelector(selector);
    if(!element) throw new Error(`Missing dashboard element: ${selector}`);
    return element;
  }

  function svgElement(name){
    return document.createElementNS(SVG_NAMESPACE, name);
  }

  function categoryIndexFor(type){
    const index = CATEGORIES.findIndex(category => category.types.has(type));
    return index >= 0 ? index : CATEGORIES.length - 1;
  }

  function eventCountsByCategory(events){
    const counts = new Array(CATEGORIES.length).fill(0);
    for(const event of events) counts[categoryIndexFor(event.type)] += 1;
    return counts;
  }

  function percentage(count, total){
    return total ? Math.round((count / total) * 100) : 0;
  }

  function eventCountLabel(count){
    return `${count} event${count === 1 ? '' : 's'}`;
  }

  function polarPoint(angle, radius=55){
    const radians = ((angle - 90) * Math.PI) / 180;
    return {
      x:60 + radius * Math.cos(radians),
      y:60 + radius * Math.sin(radians)
    };
  }

  function pieSectorPath(startAngle, endAngle){
    const boundedEnd = Math.min(endAngle, 359.999);
    const start = polarPoint(startAngle);
    const end = polarPoint(boundedEnd);
    const largeArc = boundedEnd - startAngle > 180 ? 1 : 0;
    return [
      'M 60 60',
      `L ${start.x} ${start.y}`,
      `A 55 55 0 ${largeArc} 1 ${end.x} ${end.y}`,
      'Z'
    ].join(' ');
  }

  function createOverviewChart(host){
    const plot = document.createElement('div');
    const pie = svgElement('svg');
    const tooltip = document.createElement('div');
    const legend = document.createElement('div');
    const caption = document.createElement('p');
    const segmentNodes = [];
    const legendNodes = [];
    let latestCounts = new Array(CATEGORIES.length).fill(0);
    let latestTotal = 0;
    let hoveredIndex = null;
    let focusedIndex = null;
    let defaultCaption = 'No activity yet.';

    plot.className = 'activity-overview-chart__plot';
    pie.classList.add('activity-overview-chart__pie');
    pie.setAttribute('viewBox', '0 0 120 120');
    pie.setAttribute('role', 'group');
    pie.setAttribute('aria-label', 'Activity distribution by category');
    tooltip.className = 'activity-overview-chart__tooltip';
    tooltip.id = 'activity-overview-chart-tooltip';
    tooltip.setAttribute('role', 'tooltip');
    tooltip.hidden = true;
    legend.className = 'activity-overview-chart__legend';
    caption.className = 'activity-overview-chart__caption';

    function interactionLabel(index){
      const category = CATEGORIES[index];
      const count = latestCounts[index];
      return `${category.label}: ${eventCountLabel(count)} `
        + `(${percentage(count, latestTotal)}%).`;
    }

    function syncInteraction(){
      const activeIndex = focusedIndex ?? hoveredIndex;
      const interacting = activeIndex !== null && latestCounts[activeIndex] > 0;
      pie.classList.toggle('is-interacting', interacting);
      tooltip.hidden = !interacting;
      caption.textContent = interacting ? interactionLabel(activeIndex) : defaultCaption;
      segmentNodes.forEach((segment, index) => {
        segment.classList.toggle('is-active', interacting && index === activeIndex);
      });
      legendNodes.forEach((row, index) => {
        row.classList.toggle('is-active', interacting && index === activeIndex);
      });
      if(interacting) tooltip.textContent = interactionLabel(activeIndex);
    }

    CATEGORIES.forEach((category, index) => {
      const segment = svgElement('path');
      segment.classList.add('activity-overview-chart__segment');
      segment.dataset.category = category.key;
      segment.style.fill = category.color;
      segment.setAttribute('role', 'img');
      segment.setAttribute('aria-describedby', tooltip.id);
      segment.addEventListener('pointerenter', () => {
        hoveredIndex = index;
        syncInteraction();
      });
      segment.addEventListener('pointerleave', () => {
        hoveredIndex = null;
        syncInteraction();
      });
      segment.addEventListener('focus', () => {
        focusedIndex = index;
        syncInteraction();
      });
      segment.addEventListener('blur', () => {
        focusedIndex = null;
        syncInteraction();
      });
      pie.appendChild(segment);
      segmentNodes.push(segment);

      const row = document.createElement('div');
      const swatch = document.createElement('span');
      const label = document.createElement('span');
      row.className = 'activity-overview-chart__legend-item';
      row.dataset.category = category.key;
      swatch.className = 'activity-overview-chart__legend-swatch';
      swatch.style.background = category.color;
      label.className = 'activity-overview-chart__legend-label';
      row.append(swatch, label);
      legend.appendChild(row);
      legendNodes.push(row);
    });

    plot.append(pie, tooltip);
    host.replaceChildren(plot, legend, caption);

    function update(events){
      latestTotal = events.length;
      latestCounts = eventCountsByCategory(events);
      let angle = 0;

      CATEGORIES.forEach((category, index) => {
        const count = latestCounts[index];
        const segment = segmentNodes[index];
        const row = legendNodes[index];
        const label = row.querySelector('.activity-overview-chart__legend-label');
        const startAngle = angle;
        angle += latestTotal ? (count / latestTotal) * 360 : 0;
        segment.style.display = count ? '' : 'none';
        segment.setAttribute('tabindex', count ? '0' : '-1');
        segment.setAttribute('d', count ? pieSectorPath(startAngle, angle) : '');
        segment.setAttribute('aria-label', interactionLabel(index));
        row.hidden = !count;
        label.textContent = `${category.label}: ${count} `
          + `(${percentage(count, latestTotal)}%)`;
      });

      const topCount = Math.max(...latestCounts);
      const topIndex = latestCounts.indexOf(topCount);
      defaultCaption = topCount
        ? `${eventCountLabel(latestTotal)} in this session, mostly `
          + `${CATEGORIES[topIndex].label.toLowerCase()} activity `
          + `(${percentage(topCount, latestTotal)}%).`
        : 'No activity yet.';
      plot.hidden = !latestTotal;
      legend.hidden = !latestTotal;
      syncInteraction();
    }

    function empty(message){
      latestCounts.fill(0);
      latestTotal = 0;
      hoveredIndex = null;
      focusedIndex = null;
      defaultCaption = message;
      plot.hidden = true;
      legend.hidden = true;
      syncInteraction();
    }

    return {update, empty};
  }

  function createActivityStats(host){
    const definitions = [
      ['Shell commands', events => countType(events, 'shell.command.completed')],
      ['Jobs submitted', events => countType(events, 'job.submitted')],
      ['Jobs completed', events => countType(events, 'job.completed')],
      ['Git snapshots', events => countType(events, 'git.snapshot')],
      ['File changes', events => countType(events, 'file.changed')
        + countType(events, 'file.diff')],
      ['Agent messages', events => countType(events, 'agent.message')]
    ];
    const numberNodes = definitions.map(([label]) => {
      const tile = document.createElement('div');
      const number = document.createElement('div');
      const name = document.createElement('div');
      tile.className = 'activity-overview-stats__item';
      number.className = 'activity-overview-stats__number';
      name.className = 'activity-overview-stats__label';
      name.textContent = label;
      tile.append(number, name);
      host.appendChild(tile);
      return number;
    });

    function update(events){
      host.hidden = false;
      definitions.forEach(([, getCount], index) => {
        numberNodes[index].textContent = getCount(events);
      });
    }

    function empty(){
      host.hidden = true;
      numberNodes.forEach(number => { number.textContent = '0'; });
    }

    return {update, empty};
  }

  function countType(events, type){
    return events.filter(event => event.type === type).length;
  }

  function countBy(events, keyFor){
    const counts = new Map();
    for(const event of events){
      const key = keyFor(event);
      counts.set(key, (counts.get(key) || 0) + 1);
    }
    return [...counts.entries()].sort((left, right) => right[1] - left[1]);
  }

  function renderRanking(host, entries, limit=8){
    host.replaceChildren();
    if(!entries.length){
      const empty = document.createElement('p');
      empty.className = 'activity-dashboard__empty';
      empty.textContent = 'No activity yet.';
      host.appendChild(empty);
      return;
    }
    const top = entries.slice(0, limit);
    const maximum = top[0][1] || 1;
    for(const [label, count] of top){
      const row = document.createElement('div');
      const name = document.createElement('span');
      const countElement = document.createElement('span');
      const track = document.createElement('div');
      const fill = document.createElement('div');
      row.className = 'activity-ranking__row';
      name.className = 'activity-ranking__label';
      name.textContent = label;
      name.title = label;
      countElement.className = 'activity-ranking__count';
      countElement.textContent = count;
      track.className = 'activity-ranking__track';
      fill.className = 'activity-ranking__fill';
      fill.style.width = `${Math.max(4, Math.round((count / maximum) * 100))}%`;
      row.append(name, countElement);
      track.appendChild(fill);
      host.append(row, track);
    }
  }

  function timelineModel(events, isLive, now){
    const timestamped = events
      .map(event => ({
        event,
        timestamp:new Date(event.ts).getTime(),
        sequence:Number(event.seq) || 0
      }))
      .filter(item => Number.isFinite(item.timestamp))
      .sort((left, right) => left.timestamp - right.timestamp
        || left.sequence - right.sequence);
    if(!timestamped.length) return null;

    const first = timestamped[0].timestamp;
    const lastEventTime = timestamped[timestamped.length - 1].timestamp;
    const last = isLive ? Math.max(lastEventTime, now) : lastEventTime;
    const endsAtNow = isLive && lastEventTime <= now;
    const span = Math.max(last - first, 1000);
    const bucketDuration = span / TIMELINE_BUCKET_COUNT;
    const buckets = Array.from({length:TIMELINE_BUCKET_COUNT}, (_, index) => ({
      start:first + index * bucketDuration,
      end:first + (index + 1) * bucketDuration,
      counts:new Array(CATEGORIES.length).fill(0),
      total:0
    }));

    for(const {event, timestamp} of timestamped){
      const bucketIndex = Math.min(
        TIMELINE_BUCKET_COUNT - 1,
        Math.max(0, Math.floor((timestamp - first) / bucketDuration))
      );
      const bucket = buckets[bucketIndex];
      bucket.counts[categoryIndexFor(event.type)] += 1;
      bucket.total += 1;
    }

    return {
      first,
      last,
      buckets,
      eventTotal:timestamped.length,
      endsAtNow,
      spansMultipleDays:new Date(first).toDateString() !== new Date(last).toDateString(),
      maximum:Math.max(...buckets.map(bucket => bucket.total), 1)
    };
  }

  function createTimelineChart(host, labels, summary, timeLabel, dateTimeLabel){
    const bucketNodes = [];
    const tooltip = document.createElement('div');
    let latestModel = null;
    let hoveredIndex = null;
    let focusedIndex = null;
    let rovingIndex = 0;

    tooltip.className = 'activity-timeline-chart__tooltip';
    tooltip.id = 'activity-timeline-chart-tooltip';
    tooltip.setAttribute('role', 'tooltip');
    tooltip.hidden = true;

    function timestampLabel(value){
      return latestModel?.spansMultipleDays ? dateTimeLabel(value) : timeLabel(value);
    }

    function bucketLabel(index){
      const bucket = latestModel.buckets[index];
      const breakdown = bucket.counts
        .map((count, categoryIndex) => count
          ? `${CATEGORIES[categoryIndex].label} ${count}`
          : '')
        .filter(Boolean)
        .join(', ');
      const activity = breakdown ? ` ${breakdown}.` : ' No activity.';
      return `${timestampLabel(bucket.start)} to ${timestampLabel(bucket.end)}: `
        + `${eventCountLabel(bucket.total)}.${activity}`;
    }

    function setRovingIndex(index){
      rovingIndex = index;
      bucketNodes.forEach((node, nodeIndex) => {
        node.button.tabIndex = nodeIndex === rovingIndex ? 0 : -1;
      });
    }

    function syncInteraction(){
      const activeIndex = focusedIndex ?? hoveredIndex;
      const interacting = latestModel && activeIndex !== null;
      tooltip.hidden = !interacting;
      bucketNodes.forEach((node, index) => {
        node.button.classList.toggle('is-active', interacting && index === activeIndex);
      });
      if(interacting){
        tooltip.textContent = bucketLabel(activeIndex);
        tooltip.style.setProperty(
          '--activity-timeline-tooltip-left',
          `${((activeIndex + 0.5) / TIMELINE_BUCKET_COUNT) * 100}%`
        );
      }
    }

    function handleBucketKeydown(event, index){
      let nextIndex = null;
      if(event.key === 'ArrowLeft') nextIndex = Math.max(0, index - 1);
      if(event.key === 'ArrowRight'){
        nextIndex = Math.min(TIMELINE_BUCKET_COUNT - 1, index + 1);
      }
      if(event.key === 'Home') nextIndex = 0;
      if(event.key === 'End') nextIndex = TIMELINE_BUCKET_COUNT - 1;
      if(nextIndex === null) return;
      event.preventDefault();
      setRovingIndex(nextIndex);
      bucketNodes[nextIndex].button.focus({preventScroll:true});
    }

    for(let index = 0; index < TIMELINE_BUCKET_COUNT; index += 1){
      const button = document.createElement('button');
      const stack = document.createElement('span');
      const segments = CATEGORIES.map(category => {
        const segment = document.createElement('span');
        segment.className = 'activity-timeline-chart__segment';
        segment.dataset.category = category.key;
        segment.style.background = category.color;
        stack.appendChild(segment);
        return segment;
      });
      button.type = 'button';
      button.className = 'activity-timeline-chart__bucket';
      button.setAttribute('aria-describedby', tooltip.id);
      button.tabIndex = index === rovingIndex ? 0 : -1;
      stack.className = 'activity-timeline-chart__stack';
      button.appendChild(stack);
      button.addEventListener('pointerenter', () => {
        hoveredIndex = index;
        syncInteraction();
      });
      button.addEventListener('pointerleave', () => {
        hoveredIndex = null;
        syncInteraction();
      });
      button.addEventListener('focus', () => {
        focusedIndex = index;
        setRovingIndex(index);
        syncInteraction();
      });
      button.addEventListener('blur', () => {
        focusedIndex = null;
        syncInteraction();
      });
      button.addEventListener('keydown', event => handleBucketKeydown(event, index));
      host.appendChild(button);
      bucketNodes.push({button, stack, segments});
    }
    host.appendChild(tooltip);

    function update(events, isLive, now){
      latestModel = timelineModel(events, isLive, now);
      if(!latestModel){
        empty('No timestamped activity yet.');
        return;
      }

      bucketNodes.forEach((node, index) => {
        const bucket = latestModel.buckets[index];
        const height = bucket.total
          ? Math.max(4, Math.round((bucket.total / latestModel.maximum) * 100))
          : 3;
        node.button.hidden = false;
        node.button.setAttribute('aria-label', bucketLabel(index));
        node.button.title = bucketLabel(index);
        node.stack.style.height = bucket.total ? `${height}%` : `${height}px`;
        node.stack.classList.toggle('is-empty', bucket.total === 0);
        node.segments.forEach((segment, categoryIndex) => {
          const count = bucket.counts[categoryIndex];
          segment.hidden = !count;
          segment.style.flexGrow = count;
        });
      });

      const busiestCount = latestModel.maximum;
      const busiestIndex = latestModel.buckets
        .findIndex(bucket => bucket.total === busiestCount);
      const busiest = latestModel.buckets[busiestIndex];
      const rangeEnd = latestModel.endsAtNow
        ? 'now'
        : timestampLabel(latestModel.last);
      summary.textContent = `${eventCountLabel(latestModel.eventTotal)} from `
        + `${timestampLabel(latestModel.first)} to ${rangeEnd}. `
        + `The busiest interval was ${timestampLabel(busiest.start)} to `
        + `${timestampLabel(busiest.end)}, with `
        + `${eventCountLabel(busiestCount)}.`;
      labels.replaceChildren();
      const startLabel = document.createElement('span');
      const endLabel = document.createElement('span');
      startLabel.textContent = timestampLabel(latestModel.first);
      endLabel.textContent = rangeEnd;
      labels.append(startLabel, endLabel);
      syncInteraction();
    }

    function empty(message){
      latestModel = null;
      hoveredIndex = null;
      focusedIndex = null;
      tooltip.hidden = true;
      summary.textContent = message;
      labels.replaceChildren();
      bucketNodes.forEach(node => { node.button.hidden = true; });
    }

    return {update, empty};
  }

  function create(options){
    const root = options.root;
    if(!root) throw new Error('Activity dashboard root is required.');
    const overview = createOverviewChart(requireElement(root, '#dash-overview'));
    const stats = createActivityStats(requireElement(root, '#dash-stats'));
    const timeline = createTimelineChart(
      requireElement(root, '#dash-timeline'),
      requireElement(root, '#dash-timeline-labels'),
      requireElement(root, '#dash-timeline-summary'),
      options.timeLabel,
      options.dateTimeLabel
    );
    const eventTypes = requireElement(root, '#dash-event-types');
    const topWindows = requireElement(root, '#dash-top-windows');
    const agents = requireElement(root, '#dash-agents');

    function empty(message){
      overview.empty(message);
      stats.empty();
      timeline.empty(message);
      renderRanking(eventTypes, []);
      topWindows.replaceChildren();
      agents.replaceChildren();
    }

    function render(events, context={}){
      if(!events.length){
        empty('No activity yet.');
        return;
      }
      overview.update(events);
      stats.update(events);
      timeline.update(events, Boolean(context.isLive), context.now || Date.now());
      renderRanking(eventTypes, countBy(events, event => options.eventLabel(event.type)));
      renderRanking(topWindows, countBy(
        events.filter(event => event.type === 'screen.window'),
        event => options.windowLabel(event.payload || {})
      ));
      renderRanking(agents, countBy(
        events.filter(event => event.type === 'agent.message'),
        event => options.toolLabel((event.payload || {}).tool)
      ));
    }

    return Object.freeze({empty, render});
  }

  window.WfrecActivityDashboard = Object.freeze({create, timelineModel});
})();
