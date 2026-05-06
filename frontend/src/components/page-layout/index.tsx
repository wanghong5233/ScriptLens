import classNames from 'classnames'
import { PropsWithChildren, ReactNode, type CSSProperties } from 'react'
import './index.scss'

export default function ComPageLayout(
  props: PropsWithChildren<{
    className?: string
    right?: ReactNode
    sender?: ReactNode
    style?: CSSProperties
  }>,
) {
  const { children, className, right, sender, ...rest } = props
  return (
    <div className={classNames('com-page-layout', className)} {...rest}>
      <div className="com-page-layout__main">
        <div className="com-page-layout__main-content">{children}</div>

        <div className="com-page-layout__sender">{sender}</div>
      </div>
      {right ? <div className="com-page-layout__right">{right}</div> : null}
    </div>
  )
}
