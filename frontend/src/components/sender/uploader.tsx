import { PaperClipOutlined } from '@ant-design/icons'
import { Button, Upload } from 'antd'

const ACCEPT = ['pdf', 'doc', 'docx', 'txt']
const LIMIT = 200  // 单文件最大 200MB，支持大型文档

export default function Uploader(props: {
  onFileSelected: (file: File) => void
}) {
  const { onFileSelected } = props

  return (
    <Upload
      showUploadList={false}
      maxCount={1}
      accept={ACCEPT.map((item) => `.${item}`).join(',')}
      beforeUpload={(file) => {
        // 检查后缀名
        const ext = file.name?.split('.')?.pop()?.toLowerCase() ?? ''
        const isAccept = ACCEPT.includes(ext)
        if (!isAccept) {
          window.$app.message.error(`只支持 ${ACCEPT.join('、')}`)
          return Upload.LIST_IGNORE
        }

        // 文件大小限制
        const isLimit = file.size <= LIMIT * 1024 * 1024
        if (!isLimit) {
          window.$app.message.error(`文件大小不能超过${LIMIT}M`)
          return Upload.LIST_IGNORE
        }

        // 仅将文件传递给父组件，不上传
        onFileSelected(file)
        window.$app.message.success('文件已添加')
        return false // 阻止自动上传
      }}
    >
      <Button
        className="com-sender__toolbar-icon-btn"
        type="text"
        icon={<PaperClipOutlined />}
      />
    </Upload>
  )
}
