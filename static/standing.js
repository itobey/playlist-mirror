/* Daily-run boxes.

   Without this file the control still works: it is an ordinary form with an
   ordinary submit plate, and the server answers with a redirect back to the
   record. This only removes the round trip — striking a box posts it, and the
   three things the answer changes are printed back in place.

   Nothing here decides anything. The server owns whether notification can be
   set without a daily run and what the resulting sentence says; the page
   prints what comes back, exactly as the live job view does.
*/
(function () {
  'use strict';

  var forms = document.querySelectorAll('form[data-standing]');
  if (!forms.length) return;

  var summary = document.getElementById('standing-summary');

  Array.prototype.forEach.call(forms, function (form) {
    var jobId = form.dataset.job;
    var boxes = form.querySelectorAll('input[type="checkbox"]');
    var note = form.querySelector('[data-standing-note]');
    var mark = document.querySelector('[data-order-mark="' + jobId + '"]');
    var tell = mark && mark.querySelector('[data-order-tell]');
    var scheduled = form.querySelector('input[name="scheduled"]');
    var notifyOn = form.querySelector('input[name="notify_on"]');
    var inFlight = false;

    function paint(data) {
      if (note) note.textContent = data.line || '';
      if (mark) mark.classList.toggle('is-hidden', !data.scheduled);
      if (tell) tell.classList.toggle('is-hidden', !data.notify);
      if (summary && typeof data.summary === 'string') summary.innerHTML = data.summary;
    }

    function save() {
      // Lifting the order lifts the report with it. The server does this too —
      // it is the rule, not a convenience — but doing it here as well keeps the
      // box from sitting struck for the moment the request is in the air.
      if (notifyOn && scheduled && !scheduled.checked) notifyOn.checked = false;

      inFlight = true;
      fetch(form.action, {
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
        .finally(function () {
          inFlight = false;
        });
    }

    Array.prototype.forEach.call(boxes, function (box) {
      box.addEventListener('change', function () {
        if (!inFlight) save();
      });
    });

    // Belt and braces: a keyboard submit, or a plate still on the page because
    // the stylesheet has not arrived, must not reload past a saved state.
    form.addEventListener('submit', function (event) {
      event.preventDefault();
      save();
    });
  });
})();

/* Boxes that reload instead of saving in place.

   The daily-run boxes above print their answer back into the record they sit
   on. This one governs which records exist at all, so there is nothing to print
   back — the page the server draws next is the answer. Striking it submits the
   form the ordinary way, which is also exactly what happens with no scripting
   at all; all this removes is the second click on the Set plate.
*/
(function () {
  'use strict';

  var forms = document.querySelectorAll('form[data-reload-on-change]');
  Array.prototype.forEach.call(forms, function (form) {
    var box = form.querySelector('input[type="checkbox"]');
    if (!box) return;
    box.addEventListener('change', function () {
      form.submit();
    });
  });
})();
