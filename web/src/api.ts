/**
 * HTTP 客户端的**聚合出口**（P5-2：按领域拆到下面几个文件里）。
 *
 * 老调用方 `import { fetchTree } from './api'` 一个字都不用改；
 * 新代码请直接从领域文件 import（这样依赖关系看得出来）。
 */
export * from './api-steward';
export * from './api-insight';
export * from './api-state';
export * from './api-tree';
