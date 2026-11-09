// Stand-in for "react/jsx-runtime": produces a plain tree the test can walk.

export function jsx(type, props) {
  return { type, props: props || {} }
}

export function jsxs(type, props) {
  return { type, props: props || {} }
}

export const Fragment = 'fragment'
