(function () {
  function selectedInventoryIds() {
    return Array.from(document.querySelectorAll('input.action-select:checked'))
      .map(function (checkbox) {
        return checkbox.value;
      })
      .filter(Boolean);
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
    button.textContent = 'Print selected labels';
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
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', addPrintButton);
  } else {
    addPrintButton();
  }
})();
