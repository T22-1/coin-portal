(function () {
  function selectedRowIds() {
    return Array.from(document.querySelectorAll('input.action-select:checked'))
      .map(function (checkbox) {
        return checkbox.value;
      })
      .filter(Boolean);
  }

  function isInventoryPage() {
    return document.body.classList.contains('model-inventoryitem');
  }

  function isTubePage() {
    return document.body.classList.contains('model-container');
  }

  function canPrintLabels() {
    return isInventoryPage() || isTubePage();
  }

  function isSubmissionPage() {
    return document.body.classList.contains('model-submission');
  }

  function modelLabel() {
    var heading = document.querySelector('#content h1');
    var text = heading ? heading.textContent : 'items';
    text = text.replace(/^Select\s+/i, '').replace(/\s+to\s+change$/i, '').trim();
    return text || 'items';
  }

  function setVisible(button, count) {
    if (button && button.parentElement) {
      button.parentElement.style.display = count ? '' : 'none';
    }
  }

  function updateButtons() {
    var count = selectedRowIds().length;
    var printButton = document.getElementById('print-selected-labels');
    var deleteButton = document.getElementById('delete-selected-items');
    setVisible(printButton, count);
    setVisible(deleteButton, count);
    if (printButton) {
      printButton.textContent = count === 1 ? 'Print selected label' : 'Print selected labels';
    }
    if (deleteButton) {
      deleteButton.textContent = count === 1 ? 'Delete selected item' : 'Delete selected ' + modelLabel();
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

  function addPortalAdminButtons() {
    if (!document.body.classList.contains('app-portalapp')) {
      return;
    }
    if (!document.querySelector('select[name="action"]')) {
      return;
    }

    addToolButton('delete-selected-items', 'addlink', 'Delete selected items', function (event) {
      event.preventDefault();
      var ids = selectedRowIds();
      var actionSelect = document.querySelector('select[name="action"]');
      var changelistForm = document.getElementById('changelist-form');
      if (!ids.length) {
        window.alert('Select one or more rows first.');
        return;
      }
      if (!actionSelect || !changelistForm) {
        window.alert('The delete action is not available on this page.');
        return;
      }
      if (isSubmissionPage()) {
        window.location.href = 'delete-selected/?ids=' + encodeURIComponent(ids.join(','));
        return;
      }
      actionSelect.value = 'delete_selected';
      changelistForm.submit();
    });

    if (canPrintLabels()) {
      addToolButton('print-selected-labels', 'addlink', 'Print selected labels', function (event) {
        event.preventDefault();
        var ids = selectedRowIds();
        if (!ids.length) {
          window.alert('Select one or more rows first.');
          return;
        }
        window.open('print-labels/?ids=' + encodeURIComponent(ids.join(',')), '_blank', 'noopener');
      });
    }

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

  function yearFromDateMintMark(value) {
    var match = String(value || '').match(/\b(17|18|19|20)\d{2}\b/);
    return match ? parseInt(match[0], 10) : null;
  }

  function normalizedDenomination(value) {
    return String(value || '')
      .toLowerCase()
      .replace(/\s+/g, '')
      .replace(/cents?/, 'c')
      .replace(/dollars?/, '$');
  }

  function seriesForCoin(dateMintMark, denomination) {
    var year = yearFromDateMintMark(dateMintMark);
    var denom = normalizedDenomination(denomination);
    if (!year || !denom) {
      return '';
    }

    if (['1c', 'cent', 'penny'].indexOf(denom) !== -1) {
      if (year >= 1859 && year <= 1909) return 'Indian Head Cent';
      if (year >= 1909) return 'Lincoln Cent';
    }
    if (['2c', 'twoc'].indexOf(denom) !== -1 && year >= 1864 && year <= 1873) {
      return 'Two Cent Piece';
    }
    if (['3c', 'threec'].indexOf(denom) !== -1 && year >= 1851 && year <= 1889) {
      return 'Three Cent Piece';
    }
    if (['5c', 'nickel'].indexOf(denom) !== -1) {
      if (year >= 1866 && year <= 1883) return 'Shield Nickel';
      if (year >= 1883 && year <= 1913) return 'Liberty Head Nickel';
      if (year >= 1913 && year <= 1938) return 'Buffalo Nickel';
      if (year >= 1938) return 'Jefferson Nickel';
    }
    if (['10c', 'dime'].indexOf(denom) !== -1) {
      if (year >= 1837 && year <= 1891) return 'Seated Liberty Dime';
      if (year >= 1892 && year <= 1916) return 'Barber Dime';
      if (year >= 1916 && year <= 1945) return 'Mercury Dime';
      if (year >= 1946) return 'Roosevelt Dime';
    }
    if (['20c', 'twentyc'].indexOf(denom) !== -1 && year >= 1875 && year <= 1878) {
      return 'Twenty Cent Piece';
    }
    if (['25c', 'quarter'].indexOf(denom) !== -1) {
      if (year >= 1838 && year <= 1891) return 'Seated Liberty Quarter';
      if (year >= 1892 && year <= 1916) return 'Barber Quarter';
      if (year >= 1916 && year <= 1930) return 'Standing Liberty Quarter';
      if (year >= 1932) return 'Washington Quarter';
    }
    if (['50c', 'halfdollar', 'half$'].indexOf(denom) !== -1) {
      if (year >= 1839 && year <= 1891) return 'Seated Liberty Half Dollar';
      if (year >= 1892 && year <= 1915) return 'Barber Half Dollar';
      if (year >= 1916 && year <= 1947) return 'Walking Liberty Half Dollar';
      if (year >= 1948 && year <= 1963) return 'Franklin Half Dollar';
      if (year >= 1964) return 'Kennedy Half Dollar';
    }
    if (['$1', '1$', 'dollar', '1dollar'].indexOf(denom) !== -1) {
      if (year >= 1840 && year <= 1873) return 'Seated Liberty Dollar';
      if (year >= 1878 && year <= 1921) return 'Morgan Dollar';
      if (year >= 1921 && year <= 1935) return 'Peace Dollar';
      if (year >= 1971 && year <= 1978) return 'Eisenhower Dollar';
      if (year >= 1979 && year <= 1999) return 'Susan B. Anthony Dollar';
      if (year >= 2000) return 'Sacagawea Dollar';
    }
    return '';
  }

  function addSeriesAutofill() {
    if (!isInventoryPage() && !isTubePage()) {
      return;
    }
    var denominationInput = document.getElementById('id_denomination');
    var dateInput = document.getElementById('id_date_mm');
    var seriesInput = document.getElementById('id_series');
    if (!denominationInput || !dateInput || !seriesInput) {
      return;
    }
    var labelTextInput = isTubePage() ? document.getElementById('id_label_text') : null;
    var quantityInput = isTubePage() ? document.getElementById('id_quantity') : null;

    var lastAutoSeries = seriesForCoin(dateInput.value, denominationInput.value);
    var lastAutoLabelText = tubeLabelText();

    function tubeLabelText() {
      if (!isTubePage()) {
        return '';
      }
      var parts = [
        dateInput.value.trim(),
        denominationInput.value.trim(),
        seriesInput.value.trim(),
      ].filter(Boolean);
      var label = parts.join(' ');
      var quantity = quantityInput ? quantityInput.value.trim() : '';
      if (quantity && quantity !== '0') {
        label = (label + ' QTY ' + quantity).trim();
      }
      return label;
    }

    function fillTubeLabelIfAutoManaged() {
      if (!labelTextInput) {
        return;
      }
      var currentLabelText = labelTextInput.value.trim();
      var suggestedLabelText = tubeLabelText();
      if (currentLabelText && currentLabelText !== lastAutoLabelText) {
        return;
      }
      if (suggestedLabelText) {
        labelTextInput.value = suggestedLabelText;
        lastAutoLabelText = suggestedLabelText;
      }
    }

    function fillSeriesIfAutoManaged() {
      var currentSeries = seriesInput.value.trim();
      var suggestedSeries = seriesForCoin(dateInput.value, denominationInput.value);
      if (currentSeries && currentSeries !== lastAutoSeries) {
        return;
      }
      if (suggestedSeries) {
        seriesInput.value = suggestedSeries;
        lastAutoSeries = suggestedSeries;
      }
      fillTubeLabelIfAutoManaged();
    }

    function bindAutoEvents(input, callback) {
      ['input', 'change', 'blur'].forEach(function (eventName) {
        input.addEventListener(eventName, callback);
      });
    }

    bindAutoEvents(denominationInput, fillSeriesIfAutoManaged);
    bindAutoEvents(dateInput, fillSeriesIfAutoManaged);
    bindAutoEvents(seriesInput, fillTubeLabelIfAutoManaged);
    if (quantityInput) {
      bindAutoEvents(quantityInput, fillTubeLabelIfAutoManaged);
    }
    fillSeriesIfAutoManaged();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () {
      addPortalAdminButtons();
      addSeriesAutofill();
    });
  } else {
    addPortalAdminButtons();
    addSeriesAutofill();
  }
})();
