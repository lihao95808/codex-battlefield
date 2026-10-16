
// P5-2：类型按领域拆到下面几个文件里，这里只做**聚合出口** ——
// 老调用方 `import type { X } from './types'` 一个字都不用改；新代码请直接
// 从领域文件 import（这样依赖关系看得出来）。
export * from './types-task';
export * from './types-steward';
export * from './types-state';
export * from './types-insight';
