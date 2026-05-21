import { PaperClipOutlined } from '@ant-design/icons'
import { Button, Upload } from 'antd'
import { BYTES_PER_MB, MAX_UPLOAD_SIZE_MB } from '../../constants/numbers'

const ACCEPT = ['pdf', 'doc', 'docx', 'txt']

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
        const ext = file.name?.split('.')?.pop()?.toLowerCase() ?? ''
        const isAccept = ACCEPT.includes(ext)
        if (!isAccept) {
          window.$app.message.error(`只支持 ${ACCEPT.join('、')}`)
          return Upload.LIST_IGNORE
        }

        const isLimit = file.size <= MAX_UPLOAD_SIZE_MB * BYTES_PER_MB
        if (!isLimit) {
          window.$app.message.error(`文件大小不能超过 ${MAX_UPLOAD_SIZE_MB}MB`)
          return Upload.LIST_IGNORE
        }

        onFileSelected(file)
        window.$app.message.success('文件已添加')
        return false
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
