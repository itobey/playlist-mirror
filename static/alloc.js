/* The allocation band, restruck from a stream.

   Two pages carry the band and both are now live: the job page while a playlist mirror
   it is watching runs, and the register while a daily run runs a job
   nobody is watching. The painting is identical on both, so it lives here once
   rather than twice — a meter that disagreed with itself between two tabs would
   be worse than one that did not move at all.

   Nothing here decides anything. The figures are the server's local estimate,
   the markup is already in the document, and this only restrikes it.
*/
(function () {
  'use strict';

  var pm = (window.PM = window.PM || {});

  pm.num = function (value) {
    return Number(value || 0).toLocaleString('en-US');
  };

  pm.setText = function (node, value) {
    if (node && node.textContent !== value) node.textContent = value;
  };

  pm.paintAllocation = function (alloc) {
    if (!alloc) return;
    var band = document.querySelector('.alloc');
    if (!band) return;

    var charged = Math.ceil(alloc.percent_charged);
    var cells = band.querySelectorAll('.strip__cells .cell');
    Array.prototype.forEach.call(cells, function (cell, index) {
      var n = index + 1;
      cell.classList.toggle('cell--charged', n <= charged);
      // Below 820px every second cell is hidden; an odd run would otherwise
      // display short, so the next visible cell carries the remainder.
      cell.classList.toggle('cell--charge-edge', charged % 2 === 1 && n === charged + 1);
    });

    pm.setText(document.getElementById('alloc-remaining'), pm.num(alloc.remaining));
    pm.setText(document.getElementById('alloc-videos'), pm.num(alloc.videos_affordable));

    var meter = band.querySelector('.strip__cells');
    if (meter) {
      meter.setAttribute(
        'aria-label',
        alloc.percent_charged + " percent of today's free credits used; " +
          pm.num(alloc.remaining) + ' units estimated left.'
      );
    }
  };
})();
