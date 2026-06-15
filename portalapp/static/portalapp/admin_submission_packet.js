(function () {
  function selectedSubmissionIds() {
    return Array.from(document.querySelectorAll('input.action-select:checked'))
      .map(function (checkbox) {
        return checkbox.value;
      })
      .filter(Boolean);
  }

  function updatePacketButton(button) {
    var count = selectedSubmissionIds().length;
    button.parentElement.style.display = count ? '' : 'none';
    button.textContent = count === 1 ? 'Open selected packet' : 'Open one selected packet';
  }

  function addPacketButton() {
    if (!document.body.classList.contains('model-submission')) {
      return;
    }
    if (document.getElementById('open-selected-packet')) {
      return;
    }

    var objectTools = document.querySelector('.object-tools');
    if (!objectTools) {
      return;
    }

    var item = document.createElement('li');
    var button = document.createElement('a');
    button.id = 'open-selected-packet';
    button.href = '#';
    button.className = 'addlink';
    button.addEventListener('click', function (event) {
      event.preventDefault();
      var ids = selectedSubmissionIds();
      if (ids.length !== 1) {
        window.alert('Select exactly one submission to open its packet.');
        return;
      }
      window.open('/submissions/' + encodeURIComponent(ids[0]) + '/', '_blank', 'noopener');
    });

    item.appendChild(button);
    objectTools.insertBefore(item, objectTools.firstChild);
    updatePacketButton(button);

    document.addEventListener('change', function (event) {
      if (
        event.target.matches('input.action-select') ||
        event.target.matches('#action-toggle')
      ) {
        window.setTimeout(function () {
          updatePacketButton(button);
        }, 0);
      }
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', addPacketButton);
  } else {
    addPacketButton();
  }
})();
