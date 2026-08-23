/* The register, live.

   A daily run spends free credits while nobody is on the job page. If the
   register is the page that happens to be open, it has to show that happening —
   an allocation band that only moves on reload is a band the operator has to
   distrust, and distrusting the meter is the one failure this app cannot have.

   Same contract as the job view: the server streams a snapshot, the database
   behind it is the truth, and this only restrikes markup that is already in the
   document. It writes no records, no controls and no sentences of its own.
*/
(function () {
  'use strict';

  const board = document.querySelector('[data-register]');
  if (!board) return;

  const num = window.PM.num;
  const setText = window.PM.setText;
  const summary = document.getElementById('standing-summary');

  // Which run was in flight when this page was rendered. Every control on a
  // record — whether a resume is offered at all, whether it is disabled, the
  // Watch link — is server-rendered against that answer, so a run starting or
  // ending is the one transition worth a reload.
  let runningId = board.dataset.running || '';

  // Which records this page was drawn with. A job set to come off the register
  // when it finishes takes its whole row away, and the counts, the plates and
  // the empty state around it are all server-rendered against the set that was
  // there — so a row leaving is a reload, on the same reasoning as a run
  // starting or ending.
  let rowIds = Array.prototype.map
    .call(document.querySelectorAll('[data-row]'), function (row) { return row.dataset.row; })
    .join(',');

  function editing() {
    const active = document.activeElement;
    if (!active) return false;
    const tag = active.tagName;
    return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT';
  }

  // A reload that lands mid-keystroke throws away what was being typed, so it
  // waits for the field to be given up rather than taking it away.
  let reloadPending = false;
  function reload() {
    if (!editing()) {
      window.location.reload();
      return;
    }
    if (reloadPending) return;
    reloadPending = true;
    document.addEventListener('focusout', () => {
      window.setTimeout(() => {
        if (!editing()) window.location.reload();
      }, 120);
    });
  }

  function toggleFact(row, name, value) {
    const line = row.querySelector('[data-row-' + name + '-line]');
    if (!line) return;
    line.classList.toggle('is-hidden', !value);
    setText(row.querySelector('[data-row-' + name + ']'), num(value));
  }

  function paintRow(data) {
    const row = document.querySelector('[data-row="' + data.id + '"]');
    if (!row) return;

    const stamp = row.querySelector('[data-row-stamp]');
    if (stamp && stamp.dataset.status !== data.status) {
      stamp.dataset.status = data.status;
      stamp.className = 'stamp stamp--' + data.status;
    }
    setText(row.querySelector('[data-row-stamp-label]'), data.status_label);
    setText(
      row.querySelector('[data-row-added]'),
      num(data.added) + ' of ' + num(data.copyable)
    );
    setText(row.querySelector('[data-row-units]'), num(data.units_charged));
    toggleFact(row, 'skipped', data.skipped);
    toggleFact(row, 'failed', data.failed);

    const tally = row.querySelector('[data-row-tally]');
    if (tally) {
      tally.querySelector('.tally__fill').style.setProperty('--pct', data.percent / 100);
      tally.setAttribute('aria-label', data.percent + ' percent added');
    }

    const message = row.querySelector('[data-row-message]');
    if (message) {
      setText(message, data.message || '');
      message.classList.toggle('is-hidden', !data.message);
    }

    // The two sentences under the record's controls are written by the server,
    // never here: a job's ceiling and its daily run are described in the
    // app's own words, and a second copy of those words in the browser is a
    // second place for them to go wrong.
    const mark = row.querySelector('[data-order-mark]');
    if (mark) {
      mark.classList.toggle('is-hidden', !data.scheduled);
      mark.classList.toggle('ordermark--blocked', !!data.auto_blocked);
      const tell = mark.querySelector('[data-order-tell]');
      if (tell) tell.classList.toggle('is-hidden', !data.notify);
    }

    // Notes are restruck; the boxes and the blank beside them are not. Those
    // hold what the operator is in the middle of deciding.
    setText(row.querySelector('[data-standing-note]'), data.schedule_line || '');
    const limitNote = row.querySelector('[data-limit-note]');
    if (limitNote && !limitNote.classList.contains('cap__help--warn')) {
      setText(limitNote, data.limit_line || '');
    }
  }

  function apply(data) {
    const nowRunning = data.running_id === null ? '' : String(data.running_id);
    if (nowRunning !== runningId) {
      runningId = nowRunning;
      reload();
      return;
    }

    if (Array.isArray(data.row_ids) && data.row_ids.join(',') !== rowIds) {
      rowIds = data.row_ids.join(',');
      reload();
      return;
    }

    window.PM.paintAllocation(data.allocation);
    data.jobs.forEach(paintRow);

    // How far the running job has come against its own ceiling.
    const done = document.querySelector('[data-limit-done]');
    if (done) setText(done, num(data.run_done));

    if (summary && typeof data.summary === 'string' && summary.innerHTML !== data.summary) {
      summary.innerHTML = data.summary;
    }
  }

  let source;
  function connect() {
    source = new EventSource('/events');
    source.onmessage = (event) => {
      try {
        apply(JSON.parse(event.data));
      } catch (err) {
        /* A malformed frame is not worth tearing the page down for. */
      }
    };
    source.onerror = () => {
      source.close();
      window.setTimeout(connect, 4000);
    };
  }

  connect();
  window.addEventListener('beforeunload', () => source && source.close());
})();
