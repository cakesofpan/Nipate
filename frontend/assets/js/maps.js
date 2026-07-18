/**
 * assets/js/maps.js
 * ──────────────────
 * Google Maps helpers for Nipate.
 *
 * Usage — report form (location picker)
 * ──────────────────────────────────────
 *   <div id="map"></div>
 *   <script src="assets/js/maps.js"></script>
 *   <script>
 *     window.initMap = () => NipateMaps.initPicker('map', onLocationPicked);
 *   </script>
 *   <script async src="https://maps.googleapis.com/maps/api/js?key=KEY&callback=initMap"></script>
 *
 * Usage — case detail (read-only pin)
 * ─────────────────────────────────────
 *   NipateMaps.initDisplay('map', { lat: -1.286, lng: 36.817 }, 'Last seen here');
 *
 * The Google Maps script calls window.initMap on load; each page sets that
 * callback to whichever helper it needs.
 */

const NipateMaps = (() => {

  // Kenya bounding box — default map centre
  const KENYA_CENTER = { lat: -0.023559, lng: 37.906193 };
  const DEFAULT_ZOOM  = 7;
  const PICKER_ZOOM   = 14;

  let _pickerMap    = null;
  let _pickerMarker = null;
  let _geocoder     = null;

  /* ── Location Picker (report form) ─────────────────────────── */

  /**
   * initPicker(containerId, onPickCallback)
   *
   * Renders an interactive map inside `containerId`.
   * When the user clicks or searches, `onPickCallback` is called with:
   *   { lat, lng, address, county }
   *
   * Also wires a search input with id `location-search` if present.
   */
  function initPicker(containerId, onPickCallback) {
    const el = document.getElementById(containerId);
    if (!el) { console.warn('NipateMaps: container not found:', containerId); return; }

    _geocoder = new google.maps.Geocoder();

    _pickerMap = new google.maps.Map(el, {
      center: KENYA_CENTER,
      zoom:   DEFAULT_ZOOM,
      styles: _mapStyles(),
      mapTypeControl:    false,
      streetViewControl: false,
      fullscreenControl: false,
    });

    // Try to centre on user's location
    if (navigator.geolocation) {
      navigator.geolocation.getCurrentPosition(
        pos => {
          const loc = { lat: pos.coords.latitude, lng: pos.coords.longitude };
          _pickerMap.setCenter(loc);
          _pickerMap.setZoom(PICKER_ZOOM);
        },
        () => {} // silently ignore denial
      );
    }

    // Click to drop pin
    _pickerMap.addListener('click', e => {
      const latLng = e.latLng;
      _placePickerMarker(latLng, onPickCallback);
    });

    // Wire search input if present
    const searchInput = document.getElementById('location-search');
    if (searchInput) {
      const autocomplete = new google.maps.places.Autocomplete(searchInput, {
        componentRestrictions: { country: 'ke' },
        fields: ['geometry', 'formatted_address', 'address_components'],
      });
      autocomplete.addListener('place_changed', () => {
        const place = autocomplete.getPlace();
        if (!place.geometry) return;
        const loc = place.geometry.location;
        _pickerMap.setCenter(loc);
        _pickerMap.setZoom(PICKER_ZOOM);
        _placePickerMarker(loc, onPickCallback, place.formatted_address, place.address_components);
      });
    }
  }

  function _placePickerMarker(latLng, callback, address = null, components = null) {
    if (_pickerMarker) _pickerMarker.setMap(null);

    _pickerMarker = new google.maps.Marker({
      position: latLng,
      map:      _pickerMap,
      draggable: true,
      animation: google.maps.Animation.DROP,
      icon: {
        path:        google.maps.SymbolPath.CIRCLE,
        scale:       10,
        fillColor:   '#C0392B',
        fillOpacity: 1,
        strokeColor: '#fff',
        strokeWeight: 2,
      },
    });

    _pickerMarker.addListener('dragend', e => {
      _resolveAndCallback(e.latLng, callback);
    });

    if (address && components) {
      callback({ lat: latLng.lat(), lng: latLng.lng(), address, county: _extractCounty(components) });
      _updateCoordsDisplay(latLng.lat(), latLng.lng());
    } else {
      _resolveAndCallback(latLng, callback);
    }
  }

  function _resolveAndCallback(latLng, callback) {
    const lat = typeof latLng.lat === 'function' ? latLng.lat() : latLng.lat;
    const lng = typeof latLng.lng === 'function' ? latLng.lng() : latLng.lng;
    _updateCoordsDisplay(lat, lng);

    if (!_geocoder) { callback({ lat, lng, address: '', county: '' }); return; }

    _geocoder.geocode({ location: { lat, lng } }, (results, status) => {
      if (status === 'OK' && results[0]) {
        const address    = results[0].formatted_address;
        const components = results[0].address_components;
        const county     = _extractCounty(components);
        callback({ lat, lng, address, county });

        // Auto-fill address input if present
        const searchInput = document.getElementById('location-search');
        if (searchInput && !searchInput.value) searchInput.value = address;
      } else {
        callback({ lat, lng, address: '', county: '' });
      }
    });
  }

  function _extractCounty(components) {
    for (const comp of components || []) {
      if (comp.types.includes('administrative_area_level_1')) {
        return comp.long_name.replace(' County', '').trim();
      }
    }
    return '';
  }

  function _updateCoordsDisplay(lat, lng) {
    const el = document.getElementById('map-coords-text');
    if (el) el.textContent = `${lat.toFixed(6)}, ${lng.toFixed(6)}`;
  }

  /* ── Display map (case detail — read-only) ──────────────────── */

  /**
   * initDisplay(containerId, { lat, lng }, markerLabel)
   * Renders a non-interactive map with a single pin.
   */
  function initDisplay(containerId, coords, label = 'Last seen here') {
    const el = document.getElementById(containerId);
    if (!el) return;

    const map = new google.maps.Map(el, {
      center: coords,
      zoom:   14,
      styles: _mapStyles(),
      mapTypeControl:    false,
      streetViewControl: false,
      fullscreenControl: false,
      gestureHandling:   'cooperative',
    });

    const marker = new google.maps.Marker({
      position: coords,
      map,
      title: label,
      icon: {
        path:        google.maps.SymbolPath.CIRCLE,
        scale:       10,
        fillColor:   '#C0392B',
        fillOpacity: 1,
        strokeColor: '#fff',
        strokeWeight: 2,
      },
    });

    const infoWindow = new google.maps.InfoWindow({
      content: `<div style="font-family:sans-serif;font-size:13px;padding:4px 2px">
                  <strong style="color:#C0392B">⚠ Last seen here</strong><br>
                  <span style="color:#6B7A92">${label}</span>
                </div>`,
    });
    marker.addListener('click', () => infoWindow.open(map, marker));
    infoWindow.open(map, marker);
  }

  /* ── Subtle map style ───────────────────────────────────────── */
  function _mapStyles() {
    return [
      { featureType: 'poi', elementType: 'labels', stylers: [{ visibility: 'off' }] },
      { featureType: 'transit', stylers: [{ visibility: 'simplified' }] },
      { featureType: 'road', elementType: 'geometry', stylers: [{ color: '#f5f5f5' }] },
      { featureType: 'water', elementType: 'geometry', stylers: [{ color: '#c9e8f5' }] },
      { featureType: 'landscape', stylers: [{ color: '#f9fafb' }] },
    ];
  }

  return { initPicker, initDisplay };
})();

// Default global callback (pages override this before loading the Maps script)
if (!window.initMap) {
  window.initMap = () => {};
}
