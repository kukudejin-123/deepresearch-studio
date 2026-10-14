import { useState } from "react";
import { Alert, App as AntApp, Button, Form, Input, Tabs } from "antd";
import { UserOutlined } from "@ant-design/icons";
import { loginUser, registerUser, saveAuth } from "../lib/auth";
import type { AuthUser } from "../lib/auth";

interface AuthPanelProps {
  onAuthenticated: (user: AuthUser) => void;
}

/**
 * 登录 / 注册面板
 *
 * 未携带有效 JWT 时展示；登录或注册成功后由父组件切换到研究台界面。
 */
export function AuthPanel({ onAuthenticated }: AuthPanelProps) {
  const { message } = AntApp.useApp();
  const [mode, setMode] = useState<"login" | "register">("login");
  const [loading, setLoading] = useState(false);
  const [form] = Form.useForm();

  async function handleSubmit(values: { username: string; password: string }) {
    setLoading(true);
    try {
      const result =
        mode === "login"
          ? await loginUser(values.username, values.password)
          : await registerUser(values.username, values.password);

      saveAuth(result);
      onAuthenticated(result.user);
      message.success(mode === "login" ? `欢迎回来，${result.user.username}` : `注册成功，欢迎 ${result.user.username}`);
    } catch (error) {
      message.error(error instanceof Error ? error.message : "认证失败，请重试");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="auth-shell">
      <section className="auth-panel console-panel">
        <header className="auth-heading">
          <span className="panel-kicker">DEEPSEARCH STUDIO</span>
          <h1>深研</h1>
          <p>对话式多智能体深度研究平台</p>
        </header>

        <Tabs
          activeKey={mode}
          centered
          className="auth-tabs"
          items={[
            { key: "login", label: "登录" },
            { key: "register", label: "注册" }
          ]}
          onChange={(key) => {
            setMode(key as "login" | "register");
            form.resetFields();
          }}
        />

        <Form
          form={form}
          layout="vertical"
          onFinish={handleSubmit}
          requiredMark={false}
        >
          <Form.Item
            label="用户名"
            name="username"
            rules={[
              { required: true, message: "请输入用户名" },
              { min: 3, max: 32, message: "用户名长度需在 3-32 个字符之间" },
              {
                pattern: /^[A-Za-z0-9_-]+$/,
                message: "只能包含字母、数字、下划线和连字符"
              }
            ]}
          >
            <Input
              allowClear
              placeholder="字母、数字、下划线、连字符"
              prefix={<UserOutlined aria-hidden />}
              size="large"
            />
          </Form.Item>

          <Form.Item
            label="密码"
            name="password"
            rules={[
              { required: true, message: "请输入密码" },
              { min: 6, max: 72, message: "密码长度需在 6-72 个字符之间" }
            ]}
          >
            <Input.Password
              placeholder="至少 6 位"
              size="large"
              onPressEnter={() => form.submit()}
            />
          </Form.Item>

          <Button
            block
            htmlType="submit"
            loading={loading}
            size="large"
            type="primary"
          >
            {mode === "login" ? "登录" : "注册并登录"}
          </Button>
        </Form>

        <Alert
          className="auth-hint"
          message="任务、上传文件和知识库均按用户隔离，注册即可拥有独立工作区。"
          showIcon
          type="info"
        />
      </section>
    </div>
  );
}
