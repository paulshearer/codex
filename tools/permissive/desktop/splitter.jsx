import React from 'react';

export function Splitter({ label, orientation, value, min, max, onChange }) {
  const horizontal = orientation === 'horizontal';
  function drag(event) {
    event.preventDefault();
    const handle = event.currentTarget, origin = horizontal ? event.clientY : event.clientX, initial = value;
    handle.setPointerCapture(event.pointerId);
    const move = next => onChange(Math.max(min, Math.min(max, initial + (horizontal ? next.clientY : next.clientX) - origin)));
    const stop = () => { handle.removeEventListener('pointermove', move); handle.removeEventListener('lostpointercapture', stop); };
    handle.addEventListener('pointermove', move); handle.addEventListener('lostpointercapture', stop);
  }
  return <div className={`splitter ${orientation}`} role="separator" tabIndex={0} aria-label={label}
    aria-orientation={orientation} aria-valuenow={Math.round(value)} aria-valuemin={min} aria-valuemax={max}
    onPointerDown={drag} onKeyDown={event => {
      const direction = (horizontal ? ['ArrowUp', 'ArrowDown'] : ['ArrowLeft', 'ArrowRight']).indexOf(event.key);
      if (direction >= 0 || ['Home', 'End'].includes(event.key)) {
        event.preventDefault(); onChange(event.key === 'Home' ? min : event.key === 'End' ? max : Math.max(min, Math.min(max, value + (direction ? 10 : -10))));
      }
    }} />;
}
