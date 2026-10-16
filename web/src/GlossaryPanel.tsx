/**
 * 术语表面板（P2-6）。
 *
 * 折叠式，且**默认收起**：它是参考物，不是每次都要读的东西。但标题里带上条数，
 * 让"这里有 15 个词跟日常意思不一样"本身成为一个可见的信号。
 *
 * 每条术语都把 SPEC 路径显示出来。这不是装饰：术语一旦有争议，**只认 SPEC**，
 * 界面上这句解释只是入口。少了这条路径，界面就会慢慢变成第二份真源。
 */
import { TERMS, pick } from './glossary';
import { useI18n } from './i18n';

export function GlossaryPanel() {
  const { lang, t } = useI18n();

  return (
    <details className="glossary">
      <summary>
        {t('glossary.title')}
        <span className="glossary-count">{TERMS.length}</span>
      </summary>

      <p className="onb-hint">{t('glossary.lead')}</p>

      <ul className="glossary-list">
        {TERMS.map((term) => (
          <li key={term.name.en} className="glossary-term">
            <div className="glossary-name">
              <b>{term.name.zh}</b>
              <span className="glossary-en">{term.name.en}</span>
            </div>
            <p className="glossary-meaning">{pick(term.meaning, lang)}</p>
            <p className="glossary-interop">
              <span className="glossary-tag">{t('glossary.col.interop')}</span>
              {pick(term.interop, lang)}
            </p>
            <code className="glossary-spec" title={term.spec}>
              {term.spec}
            </code>
          </li>
        ))}
      </ul>

      <p className="onb-hint">{t('glossary.note')}</p>
    </details>
  );
}
