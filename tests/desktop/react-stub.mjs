// Stand-in for "react": hooks that tolerate being called outside a renderer, so
// component bodies can be executed by the test's tree walker.

export function useState(initial) {
  const value = typeof initial === 'function' ? initial() : initial
  return [value, () => {}]
}

export function useReducer(_reducer, initial) {
  return [initial, () => {}]
}

export function useEffect() {}

export function useLayoutEffect() {}

export function useCallback(fn) {
  return fn
}

export function useMemo(fn) {
  return fn()
}

export function useRef(initial) {
  return { current: initial }
}

export function createElement(type, props, ...children) {
  return { type, props: { ...(props || {}), children: children.length === 1 ? children[0] : children } }
}

export default { createElement }
