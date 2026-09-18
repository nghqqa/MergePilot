// frontend/src/icons.jsx — one drawn icon system, uniform 1.7px stroke, 16px grid.
import React from 'react';

const I = ({ children, size = 15, viewBox = '0 0 16 16', ...rest }) => (
  <svg
    width={size}
    height={size}
    viewBox={viewBox}
    fill="none"
    stroke="currentColor"
    strokeWidth="1.7"
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
    focusable="false"
    {...rest}
  >
    {children}
  </svg>
);

export const IconPlay = (p) => (
  <I {...p}><path d="M4.5 2.8v10.4L13 8 4.5 2.8z" fill="currentColor" stroke="none" /></I>
);
export const IconPause = (p) => (
  <I {...p}><path d="M5 3v10M11 3v10" strokeWidth="2.4" /></I>
);
export const IconNext = (p) => (
  <I {...p}><path d="M2.5 3.2v9.6L9.5 8 2.5 3.2z" fill="currentColor" stroke="none" /><path d="M12 3v10" strokeWidth="2.2" /></I>
);
export const IconPrev = (p) => (
  <I {...p}><path d="M13.5 3.2v9.6L6.5 8l7-4.8z" fill="currentColor" stroke="none" /><path d="M4 3v10" strokeWidth="2.2" /></I>
);
export const IconSkipEnd = (p) => (
  <I {...p}><path d="M2 3.4v9.2L7.5 8 2 3.4z" fill="currentColor" stroke="none" /><path d="M8.5 3.4v9.2L14 8 8.5 3.4z" fill="currentColor" stroke="none" /></I>
);
export const IconReset = (p) => (
  <I {...p}><path d="M3 8a5 5 0 1 0 1.7-3.8" /><path d="M3 2.6v3h3" /></I>
);
export const IconGate = (p) => (
  <I {...p}><path d="M8 1.8l5 2v3.4c0 3.2-2 5.6-5 7-3-1.4-5-3.8-5-7V3.8l5-2z" /><path d="M5.6 7.8l1.7 1.7 3.1-3.1" /></I>
);
export const IconLock = (p) => (
  <I {...p}><rect x="3.5" y="7" width="9" height="6.5" rx="1.2" /><path d="M5.5 7V5a2.5 2.5 0 0 1 5 0v2" /></I>
);
export const IconUnlock = (p) => (
  <I {...p}><rect x="3.5" y="7" width="9" height="6.5" rx="1.2" /><path d="M5.5 7V5a2.5 2.5 0 0 1 5-.2" /></I>
);
export const IconShieldCheck = (p) => (
  <I {...p}><path d="M8 1.8l5 2v3.4c0 3.2-2 5.6-5 7-3-1.4-5-3.8-5-7V3.8l5-2z" /><path d="M5.8 8l1.6 1.6 2.8-2.9" /></I>
);
export const IconAlert = (p) => (
  <I {...p}><path d="M8 2.2L14.5 13h-13L8 2.2z" /><path d="M8 6.4v3" /><path d="M8 11.4v.01" strokeWidth="2.2" /></I>
);
export const IconCheck = (p) => <I {...p}><path d="M3 8.6l3.2 3.2L13 4.6" /></I>;
export const IconX = (p) => <I {...p}><path d="M4 4l8 8M12 4l-8 8" /></I>;
export const IconDot = ({ color = 'currentColor', size = 8 }) => (
  <svg width={size} height={size} viewBox="0 0 8 8" aria-hidden="true"><circle cx="4" cy="4" r="3.4" fill={color} /></svg>
);
export const IconArrow = (p) => <I {...p}><path d="M3 8h10M9.5 4.5L13 8l-3.5 3.5" /></I>;
export const IconBranch = (p) => (
  <I {...p}><circle cx="4.5" cy="3.5" r="1.7" /><circle cx="4.5" cy="12.5" r="1.7" /><circle cx="11.5" cy="6.5" r="1.7" /><path d="M4.5 5.2v5.6M6.1 4.2c3 .4 4 1 4.9 1.6M6.1 11.6c3-.5 4-2.4 4.7-3.7" /></I>
);
export const IconPr = (p) => (
  <I {...p}><circle cx="4" cy="3.8" r="1.6" /><circle cx="4" cy="12.2" r="1.6" /><circle cx="12" cy="12.2" r="1.6" /><path d="M4 5.4v5.2M12 10.6V6.4a2 2 0 0 0-2-2H8.6" /><path d="M10.2 2.8L8.4 4.4l1.8 1.6" /></I>
);
export const IconBox = (p) => (
  <I {...p}><path d="M8 1.8l5.5 3v6.4l-5.5 3-5.5-3V4.8l5.5-3z" /><path d="M2.7 4.9L8 7.9l5.3-3M8 7.9v6.2" /></I>
);
export const IconUsers = (p) => (
  <I {...p}><circle cx="5.8" cy="5.3" r="2.3" /><path d="M1.8 13.4c.4-2.4 2-3.7 4-3.7s3.6 1.3 4 3.7" /><path d="M10.4 3.3a2.3 2.3 0 0 1 0 4.1M11.6 9.9c1.4.5 2.3 1.7 2.6 3.5" /></I>
);
export const IconGear = (p) => (
  <I {...p}><circle cx="8" cy="8" r="2.2" /><path d="M8 1.5v2M8 12.5v2M1.5 8h2M12.5 8h2M3.4 3.4l1.4 1.4M11.2 11.2l1.4 1.4M12.6 3.4l-1.4 1.4M4.8 11.2l-1.4 1.4" /></I>
);
export const IconFilm = (p) => (
  <I {...p}><rect x="1.8" y="3" width="12.4" height="10" rx="1.4" /><path d="M4.6 3v10M11.4 3v10M1.8 6.4h2.8M1.8 9.6h2.8M11.4 6.4h2.8M11.4 9.6h2.8" /></I>
);
export const IconStage = (p) => (
  <I {...p}><path d="M2 13.5h12" /><path d="M4.5 13.5V6l3.5-3 3.5 3v7.5" /><path d="M6.7 13.5V10h2.6v3.5" /></I>
);
export const IconDoc = (p) => (
  <I {...p}><path d="M4 1.8h5.5L13 5.3V14a.8.8 0 0 1-.8.8H4.8A.8.8 0 0 1 4 14V1.8z" /><path d="M9.3 1.8v3.9H13M6.2 8h4M6.2 10.6h4" /></I>
);
export const IconHash = (p) => (
  <I {...p}><path d="M6.2 2.2L4.8 13.8M11.2 2.2L9.8 13.8M2.6 5.8h11M2.2 10.2h11" /></I>
);
