/**
 * 语言开关（P2-6）。
 *
 * 做成**两个真实按钮**而不是一个下拉框：一来两种语言下都能点（下拉框在你不懂的
 * 语言里打开会一脸茫然），二来 `aria-pressed` 让"当前是哪一种"对屏幕阅读器也成立。
 *
 * 用 `role="group"` + `aria-label`，与既有的运行模式开关保持同一套写法。
 */
import { LANGS, LANG_LABEL, useI18n } from './i18n';

export function LanguageSwitch() {
  const { lang, setLang, t } = useI18n();

  return (
    <div className="lang-switch" role="group" aria-label={t('lang.label')}>
      {LANGS.map((item) => (
        <button
          key={item}
          type="button"
          className={item === lang ? 'active' : ''}
          aria-pressed={item === lang}
          title={item === 'zh' ? t('lang.switchTo.zh') : t('lang.switchTo.en')}
          onClick={() => setLang(item)}
        >
          {LANG_LABEL[item]}
        </button>
      ))}
    </div>
  );
}
