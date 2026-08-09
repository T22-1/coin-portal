(function () {
  function selectedInventoryIds() {
    return Array.from(document.querySelectorAll('input.action-select:checked'))
      .map(function (checkbox) {
        return checkbox.value;
      })
      .filter(Boolean);
  }

  function setVisible(button, count) {
    if (button && button.parentElement) {
      button.parentElement.style.display = count ? '' : 'none';
    }
  }

  function updateButtons() {
    var count = selectedInventoryIds().length;
    var printButton = document.getElementById('print-selected-labels');
    var deleteButton = document.getElementById('delete-selected-items');
    setVisible(printButton, count);
    setVisible(deleteButton, count);
    if (printButton) {
      printButton.textContent = count === 1 ? 'Print selected label' : 'Print selected labels';
    }
    if (deleteButton) {
      deleteButton.textContent = count === 1 ? 'Delete selected item' : 'Delete selected items';
    }
  }

  function addToolButton(id, className, label, clickHandler) {
    if (document.getElementById(id)) {
      return;
    }
    var objectTools = document.querySelector('.object-tools');
    if (!objectTools) {
      return;
    }
    var item = document.createElement('li');
    var button = document.createElement('a');
    button.id = id;
    button.href = '#';
    button.className = className;
    button.textContent = label;
    button.addEventListener('click', clickHandler);
    item.appendChild(button);
    objectTools.insertBefore(item, objectTools.firstChild);
  }

  function addInventoryButtons() {
    if (!document.body.classList.contains('model-inventoryitem')) {
      return;
    }

    addToolButton('delete-selected-items', 'addlink', 'Delete selected items', function (event) {
      event.preventDefault();
      var ids = selectedInventoryIds();
      var actionSelect = document.querySelector('select[name="action"]');
      var changelistForm = document.getElementById('changelist-form');
      if (!ids.length) {
        window.alert('Select one or more inventory items first.');
        return;
      }
      if (!actionSelect || !changelistForm) {
        window.alert('The delete action is not available on this page.');
        return;
      }
      actionSelect.value = 'delete_selected';
      changelistForm.submit();
    });

    addToolButton('print-selected-labels', 'addlink', 'Print selected labels', function (event) {
      event.preventDefault();
      var ids = selectedInventoryIds();
      if (!ids.length) {
        window.alert('Select one or more inventory items first.');
        return;
      }
      window.open('print-labels/?ids=' + encodeURIComponent(ids.join(',')), '_blank', 'noopener');
    });

    updateButtons();

    document.addEventListener('change', function (event) {
      if (
        event.target.matches('input.action-select') ||
        event.target.matches('#action-toggle')
      ) {
        window.setTimeout(updateButtons, 0);
      }
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', addInventoryButtons);
  } else {
    addInventoryButtons();
  }
})();
