/* The run ceiling.

   The daily run's script, applied to the control beside it, and for the
   same reasons: without this file the ceiling still works — it is an ordinary
   form with an ordinary submit plate, and the server answers with a redirect
   back to the record. This only removes the round trip.

   Nothing here decides anything. The server owns what a ceiling of a given size
   will actually do to the next run and what the resulting sentence says; the
   page prints what comes back.
*/
(function () {
  'use strict';

  var forms = document.querySelectorAll('form[data-limit]');
  if (!forms.length) return;

  // A run reads its ceiling once, when it starts, so a save still in the air
  // when Start is pressed would let the run begin under the old figure. The
  // plate waits for the save rather than racing it.
  var saving = null;

  var starter = document.querySelector('form[data-await-limit]');
  if (starter) {
    starter.addEventListener('submit', function (event) {
      if (!saving) return;
      event.preventDefault();
      saving.then(function () {
        starter.submit();
      });
    });
  }

  Array.prototype.forEach.call(forms, function (form) {
    var box = form.querySelector('input[name="limit_on"]');
    var figure = form.querySelector('input[name="run_limit"]');
    var note = form.querySelector('[data-limit-note]');
    var jobId = form.dataset.job;

    // Struck rather than enterable: this job is running and its ceiling is
    // already in force. Printed, not editable, and nothing to save.
    if (!box || box.disabled) return;

    // The ceiling is quoted inside the daily run's own sentence and in the
    // plan ledger, so both are restruck from the same answer rather than left
    // saying what was true a moment ago.
    var scheduleNote = document.querySelector(
      'form[data-standing][data-job="' + jobId + '"] [data-standing-note]'
    );
    var capLine = document.querySelector('[data-cap-line]');
    var capFigure = document.getElementById('p-cap');

    function warn(message) {
      if (!note) return;
      note.textContent = message;
      note.classList.add('cap__help--warn');
    }

    function paint(data) {
      if (data.error) {
        warn(data.error);
        return;
      }
      if (note) {
        note.textContent = data.line || '';
        note.classList.remove('cap__help--warn');
      }
      if (scheduleNote && typeof data.schedule_line === 'string') {
        scheduleNote.textContent = data.schedule_line;
      }
      if (capLine) {
        capLine.classList.toggle('is-hidden', !data.run_limit);
        if (capFigure && data.run_limit) {
          var first = capFigure.firstChild;
          var struck = Number(data.stops_after || 0).toLocaleString('en-US');
          // The figure shares its cell with a unit legend, so only the leading
          // text node may be rewritten.
          if (first && first.nodeType === 3) first.nodeValue = struck;
        }
      }
    }

    function save() {
      saving = fetch(form.action, {
        method: 'POST',
        headers: { Accept: 'application/json' },
        body: new FormData(form)
      })
        .then(function (response) {
          if (!response.ok) throw new Error(response.status);
          return response.json();
        })
        .then(paint)
        .catch(function () {
          // The one thing worse than a slow control is a lying one: if the
          // server never heard this, submit the form properly so the next page
          // is drawn from what was actually stored.
          form.submit();
        })
        .then(function () {
          saving = null;
        });
    }

    box.addEventListener('change', save);
    // `change` rather than `input`: the figure is typed, not stepped, and saving
    // on every keystroke would store 5 on the way to 50.
    if (figure) figure.addEventListener('change', save);

    form.addEventListener('submit', function (event) {
      event.preventDefault();
      save();
    });
  });
})();
