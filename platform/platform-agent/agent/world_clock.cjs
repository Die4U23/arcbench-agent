'use strict';
// Local controller preload: business Date is fixed, timers stay real.
const NativeDate = globalThis.Date;
const fixed = NativeDate.parse(process.env.ARCBENCH_FIXED_TIME);
if (!Number.isFinite(fixed)) throw new Error('ARCBENCH_FIXED_TIME is invalid');
function BusinessDate(...args) {
  if (!new.target) return new NativeDate(fixed).toString();
  return Reflect.construct(NativeDate, args.length ? args : [fixed], new.target);
}
Object.setPrototypeOf(BusinessDate, NativeDate);
BusinessDate.prototype = NativeDate.prototype;
Object.defineProperty(BusinessDate, 'now', {value: () => fixed});
globalThis.Date = BusinessDate;
