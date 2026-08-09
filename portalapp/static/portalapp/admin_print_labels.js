(function () {
  function selectedInventoryIds() {
    return Array.from(document.querySelectorAll('input.action-select:checked'))
      .map(function (checkbox) {
        return checkbox.value;
      })
      .filter(Boolean);
  }

  function updateSelectionButtons(printButton, deleteButton) {
    var count = selectedInventoryIds().length;
    printButton.parentElement.style.display = count ? '' : 'none';
    deleteButton.parentElement.style.display = count ? '' : 'none';
    printButton.textContent = count === 1 ? 'Print selected label' : 'Print selected labels';
    deleteButton.textContent = count === 1 ? 'Delete selected item' : 'Delete selected items';
  }

  function addInventoryButtons() {
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

    var printItem = document.createElement('li');
    var printButton = document.createElement('a');
    printButton.id = 'print-selected-labels';
    printButton.href = '#';
    printButton.className = 'addlink';
    printButton.addEventListener('click', function (event) {
      event.preventDefault();
      var ids = selectedInventoryIds();
      if (!ids.length) {
        window.alert('Select one or more inventory items first.');
        return;
      }
      window.open('print-labels/?ids=' + encodeURIComponent(ids.join(',')), '_blank', 'noopener');
    });

    var deleteItem = document.createElement('li');
    var deleteButton = document.createElement('a');
    deleteButton.id = 'delete-selected-items';
    deleteButton.href = '#';
    deleteButton.className = 'deletelink';
    deleteButton.addEventListener('click', function (event) {
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

    printItem.appendChild(printButton);
    deleteItem.appendChild(deleteButton);
    objectTools.insertBefore(deleteItem, objectTools.firstChild);
    objectTools.insertBefore(printItem, objectTools.firstChild);
    updateSelectionButtons(printButton, deleteButton);

    document.addEventListener('change', function (event) {
      if (
        event.target.matches('input.action-select') ||
        event.target.matches('#action-toggle')
      ) {
        window.setTimeout(function () {
          updateSelectionButtons(printButton, deleteButton);
        }, 0);
      }
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', addInventoryButtons);
  } else {
    addInventoryButtons();
  }
})();
