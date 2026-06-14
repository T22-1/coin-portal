(function () {
  function selectedInventoryIds() {
    return Array.from(document.querySelectorAll('input.action-select:checked'))
      .map(function (checkbox) {
        return checkbox.value;
      })
      .filter(Boolean);
  }

  function updatePrintButton(button) {
    var count = selectedInventoryIds().length;
    button.parentElement.style.display = count ? '' : 'none';
    button.textContent = count === 1 ? 'Print selected label' : 'Print selected labels';
  }

  function addPrintButton() {
    if (!document.body.classList.contains('model-inventoryitem')) {
      return;
    }
    if (document.getElementById('print-selected-labels')) {
      return;
    }

    var objectTools = document.querySelector('.object-tools');
    if (!objectTools) {
      return;
    }

    var item = document.createElement('li');
    var button = document.createElement('a');
    button.id = 'print-selected-labels';
    button.href = '#';
    button.className = 'addlink';
    button.addEventListener('click', function (event) {
      event.preventDefault();
      var ids = selectedInventoryIds();
      if (!ids.length) {
        window.alert('Select one or more inventory items first.');
        return;
      }
      window.open('print-labels/?ids=' + encodeURIComponent(ids.join(',')), '_blank', 'noopener');
    });

    item.appendChild(button);
    objectTools.insertBefore(item, objectTools.firstChild);
    updatePrintButton(button);

    document.addEventListener('change', function (event) {
      if (
        event.target.matches('input.action-select') ||
        event.target.matches('#action-toggle')
      ) {
        window.setTimeout(function () {
          updatePrintButton(button);
        }, 0);
      }
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', addPrintButton);
  } else {
    addPrintButton();
  }
})();
