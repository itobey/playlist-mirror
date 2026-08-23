/* Live job view.

   The server streams a full snapshot; the database behind it is the truth, so a
   reload, a second tab or a dropped connection all recover on their own. This
   script only prints what arrives: it never derives job state locally.
*/
(function () {
  'use strict';

  const block = document.getElementById('job-block');
  if (!block) return;

  const jobId = block.dataset.jobId;
  const MAX_ROWS = 400;

  const el = (id) => document.getElementById(id);
  const nodes = {
    stamp: el('job-stamp'),
    stampLabel: el('job-stamp-label'),
    total: el('f-total'),
    added: el('f-added'),
    pending: el('f-pending'),
    skipped: el('f-skipped'),
    charged: el('f-charged'),
    tally: el('job-tally'),
    percent: el('job-percent'),
    message: el('job-message'),
    phase: el('listing-phase'),
    phaseLabel: document.querySelector('.listing__phase-label'),
    rate: el('listing-rate'),
    eta: el('listing-eta'),
    records: el('records'),
    running: el('running-line'),
    runningPos: el('running-pos'),
    runningTitle: el('running-title'),
    empty: el('listing-empty'),
    planLeft: el('p-left'),
    planUnits: el('p-units'),
    planCovers: el('p-covers'),
    planDays: el('p-days'),
    planCap: el('p-cap'),
    capLine: document.querySelector('[data-cap-line]'),
    limitDone: document.querySelector('[data-limit-done]')
  };

  const STATUS_LABELS = {
    pending: 'SUBMITTED',
    running: 'RUNNING',
    held_quota: 'PAUSED — NO CREDITS LEFT',
    held_limit: 'PAUSED — LIMIT REACHED',
    complete: 'COMPLETE',
    failed: 'FAILED',
    cancelled: 'STOPPED'
  };

  // Keyed on video id, not sequence: re-enumerating a channel that has published
  // since renumbers positions, and a position key would reprint every line.
  const printed = new Set();
  Array.prototype.forEach.call(nodes.records.children, (li) => {
    printed.add(li.dataset.vid);
  });

  const num = window.PM.num;
  const pad = (value) => String(value).padStart(4, '0');
  const setText = window.PM.setText;

  // A ledger figure is printed with its unit legend beside it inside the same
  // cell, so only the leading text node may be rewritten — writing textContent
  // would take the legend with it and leave a bare number in the column.
  function setFigure(node, value) {
    if (!node) return;
    const first = node.firstChild;
    if (first && first.nodeType === Node.TEXT_NODE) {
      if (first.nodeValue !== value) first.nodeValue = value;
    } else {
      node.insertBefore(document.createTextNode(value), first);
    }
  }

  function icon(name) {
    const paths = {
      check: '<path d="M2.5 8.5 L6 12 L13.5 4"/>',
      cross: '<path d="M3.5 3.5 L12.5 12.5 M12.5 3.5 L3.5 12.5"/>'
    };
    return (
      '<svg class="ico" viewBox="0 0 16 16" width="16" height="16" fill="none" ' +
      'stroke="currentColor" stroke-width="1.5" stroke-linecap="square" aria-hidden="true">' +
      paths[name] +
      '</svg>'
    );
  }

  function rowFor(record) {
    const li = document.createElement('li');
    li.className = 'rec rec--' + record.state + ' rec--fresh';
    li.dataset.vid = record.video_id;
    const added = record.state === 'added';
    li.innerHTML =
      '<span class="rec__pos">' + pad(record.position) + '</span>' +
      '<span class="rec__state">' + icon(added ? 'check' : 'cross') +
      '<span>' + (added ? 'added' : 'skipped') + '</span></span>' +
      '<span class="rec__title"></span>' +
      '<span class="rec__id"></span>' +
      (record.message ? '<span class="rec__msg"></span>' : '');
    li.querySelector('.rec__title').textContent = record.title || record.video_id;
    li.querySelector('.rec__id').textContent = record.video_id;
    if (record.message) li.querySelector('.rec__msg').textContent = record.message;
    return li;
  }

  function nearBottom() {
    const slack = 260;
    return window.innerHeight + window.scrollY >= document.body.offsetHeight - slack;
  }

  function printRecords(records) {
    // The stream sends newest first; the form prints downward, so replay in
    // the order the printer would have struck them.
    const ordered = records.slice().reverse();
    const follow = nearBottom();
    let appended = 0;
    ordered.forEach((record) => {
      if (printed.has(record.video_id)) return;
      printed.add(record.video_id);
      const row = rowFor(record);
      // A batch of records arrives in one frame; stagger them so the form
      // advances line by line the way a printer does, not as a pasted block.
      row.style.setProperty('--i', appended);
      nodes.records.appendChild(row);
      appended += 1;
    });
    if (!appended) return;

    while (nodes.records.children.length > MAX_ROWS) {
      const first = nodes.records.firstElementChild;
      printed.delete(first.dataset.vid);
      nodes.records.removeChild(first);
    }
    nodes.empty.classList.add('is-hidden');
    if (follow) nodes.running.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }

  let wasRunning = block.dataset.wasRunning === 'true';

  function apply(data) {
    const job = data.job;

    // Controls depend on whether a run is in flight, and those are rendered
    // server-side. A transition across that line is the one thing worth a reload.
    if (wasRunning !== job.is_running) {
      window.location.reload();
      return;
    }

    if (nodes.stamp.dataset.status !== job.status) {
      nodes.stamp.dataset.status = job.status;
      nodes.stamp.className = 'stamp stamp--' + job.status;
    }
    setText(nodes.stampLabel, STATUS_LABELS[job.status] || job.status.toUpperCase());

    setText(nodes.total, num(job.total));
    setText(nodes.added, num(job.added));
    setText(nodes.pending, num(job.pending + job.failed));
    setText(nodes.skipped, num(job.skipped));
    setText(nodes.charged, num(job.units_charged));

    if (nodes.tally) {
      nodes.tally.querySelector('.tally__fill').style.setProperty('--pct', job.percent / 100);
      nodes.tally.setAttribute('aria-label', job.percent + ' percent added');
      setText(nodes.percent, job.percent + '%');
    }

    if (job.message) {
      setText(nodes.message, job.message);
      nodes.message.classList.remove('is-hidden');
    } else {
      nodes.message.classList.add('is-hidden');
    }

    if (nodes.planLeft) {
      setFigure(nodes.planLeft, num(job.pending + job.failed));
      setFigure(nodes.planUnits, num(job.units_remaining_for_job));
      setFigure(nodes.planCovers, num(data.allocation.videos_affordable));
      setFigure(nodes.planDays, String(job.days_required));
      // The ceiling's line is always in the document, because the ceiling is set
      // from a control that saves without a reload.
      if (nodes.capLine) {
        nodes.capLine.classList.toggle('is-hidden', !job.run_limit);
        if (job.run_limit) setFigure(nodes.planCap, num(job.run_stops_after));
      }
    }

    // Allocation line. Shared with the register, which paints the same band
    // from its own stream.
    window.PM.paintAllocation(data.allocation);

    // The ceiling, while the run it governs is going. It reads the ceiling once
    // at the start, so the figure beside it is what has been settled against it.
    if (nodes.limitDone) setText(nodes.limitDone, num(data.live.settled_this_run));

    // Print head.
    if (job.is_running) {
      nodes.phase.classList.remove('is-hidden');
      setText(nodes.phaseLabel, data.live.phase || '');
      setText(nodes.rate, data.live.rate ? data.live.rate + ' per minute' : '');
      setText(nodes.eta, data.eta.text || '');
      if (data.next_record) {
        nodes.running.classList.remove('is-hidden');
        setText(nodes.runningPos, pad(data.next_record.position));
        setText(nodes.runningTitle, data.next_record.title);
      } else {
        nodes.running.classList.add('is-hidden');
      }
    } else {
      nodes.phase.classList.add('is-hidden');
      nodes.running.classList.add('is-hidden');
    }

    printRecords(data.records);
  }

  let source;
  function connect() {
    source = new EventSource('/jobs/' + jobId + '/events');
    source.onmessage = (event) => {
      try {
        apply(JSON.parse(event.data));
      } catch (err) {
        /* A malformed frame is not worth tearing the page down for. */
      }
    };
    // The job is not on the register any more. It may have been removed from
    // another tab, or — with removal-on-completion set — it may have just
    // finished under the eyes watching it, in which case this is the only time
    // its result will ever be printed. The server sends the sentence and the
    // playlist with the event; the page only carries them across.
    source.addEventListener('gone', (event) => {
      source.close();
      let farewell = {};
      try {
        farewell = JSON.parse(event.data || '{}');
      } catch (err) {
        /* No farewell is still a valid answer: the job is gone either way. */
      }
      let target = '/';
      if (farewell.notice) {
        target += '?notice=' + encodeURIComponent(farewell.notice);
        if (farewell.playlist_url) {
          target += '&notice_url=' + encodeURIComponent(farewell.playlist_url);
        }
      }
      window.location.replace(target);
    });
    source.onerror = () => {
      source.close();
      window.setTimeout(connect, 4000);
    };
  }

  connect();
  window.addEventListener('beforeunload', () => source && source.close());
})();
