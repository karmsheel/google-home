// Stand-in for @hermes/plugin-sdk. Exposes exactly the names googlehome/desktop/plugin.js
// imports, with atoms and hooks that work outside React so components can be rendered.

function makeAtom(initial) {
  return {
    value: initial,
    get() {
      return this.value
    },
    set(next) {
      this.value = next
    },
    subscribe() {
      return () => {}
    }
  }
}

export function atom(initial) {
  return makeAtom(initial)
}

export function useValue(atomRef) {
  return atomRef && typeof atomRef.get === 'function' ? atomRef.get() : atomRef
}

export function haptic() {}

export const ROUTES_AREA = 'routes'
export const SIDEBAR_NAV_AREA = 'sidebar.nav'
export const PALETTE_AREA = 'palette'
export const STATUSBAR_AREAS = { left: 'statusBar.left', right: 'statusBar.right' }

export const host = {
  state: {
    profile: makeAtom('coder'),
    gateway: makeAtom('open')
  },
  navigate() {},
  notify() {},
  notifyError() {},
  request: async () => ({}),
  requestProfile: async () => ({ text: '' }),
  profiles: {
    list: async () => ({ profiles: [{ name: 'coder' }, { name: 'designer' }] })
  }
}

function component(name) {
  const Comp = props => ({ type: 'div', props: { ...props, 'data-component': name } })
  Comp.displayName = name
  return Comp
}

export const Button = component('Button')
export const ListRow = component('ListRow')
export const Select = component('Select')
export const SelectContent = component('SelectContent')
export const SelectItem = component('SelectItem')
export const SelectTrigger = component('SelectTrigger')
export const SelectValue = component('SelectValue')
export const Tip = component('Tip')
