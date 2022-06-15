import { Buffer } from 'buffer';

import { request } from '@octokit/request';
import { Formik } from 'formik';
import PropTypes from 'prop-types';
import React, { useState, useEffect, useRef } from 'react';
import {
  Button, Form, Modal, Container, Spinner, Alert, Breadcrumb,
} from 'react-bootstrap';
import { Helmet } from 'react-helmet';
import { BrowserRouter, useLocation } from 'react-router-dom';

import Favicon from './favicon.ico';
import LogoMergify from './logo-mergify-black.png';

import './App.css';

const getRandomString = function getRandomString(size) {
  const randomBytes = new Uint8Array(size / 2);
  (window.crypto || window.msCrypto).getRandomValues(randomBytes);
  return Array.from(randomBytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
};

const useAppCreator = function useAppCreator(code) {
  const [info, setInfo] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => {
    if (!code) {
      return;
    }
    const run = async () => {
      let respApp;
      let respBot;
      try {
        respApp = await request(`POST /app-manifests/${code}/conversions`);
        respBot = await request(`GET /users/${respApp.data.slug}[bot]`);
      } catch (exc) {
        if (exc.status === 404) {
          setError('The `code` is not valid anymore.');
        } else {
          const message = (exc.status ? `HTTP code ${exc.status}: ${exc.message}` : exc);
          setError(`Unexpeced error: ${message}`);
        }
        return;
      }
      setInfo({
        app: respApp.data,
        config: `
MERGIFYENGINE_BASE_URL=${window.location.origin}
MERGIFYENGINE_INTEGRATION_ID=${respApp.data.id}
MERGIFYENGINE_BOT_USER_ID=${respBot.data.id}
MERGIFYENGINE_BOT_USER_LOGIN=${respBot.data.login}
MERGIFYENGINE_OAUTH_CLIENT_ID=${respApp.data.client_id}
MERGIFYENGINE_OAUTH_CLIENT_SECRET=${respApp.data.client_secret}
MERGIFYENGINE_WEBHOOK_SECRET=${respApp.data.webhook_secret}
MERGIFYENGINE_PRIVATE_KEY=${Buffer.from(respApp.data.pem).toString('base64')}
MERGIFYENGINE_CACHE_TOKEN_SECRET=${getRandomString(42)}
`,
      });
    };
    run();
  }, [code, setInfo]);

  return [info, error];
};

const Steper = function Steper(props) {
  const { step } = props;
  return (
    <Breadcrumb>
      <Breadcrumb.Item active className={step === 1 ? 'fw-bold' : ''}>1. Creation of the GitHub App</Breadcrumb.Item>
      <Breadcrumb.Item active className={step === 2 ? 'fw-bold' : ''}>2. Installation of the GitHub App</Breadcrumb.Item>
      <Breadcrumb.Item active className={step === 3 ? 'fw-bold' : ''}>3. Installation completed</Breadcrumb.Item>
    </Breadcrumb>
  );
};
Steper.propTypes = {
  step: PropTypes.number.isRequired,
};

const StepCreateGitHubApp = function StepCreateGitHubApp() {
  const manifestInput = useRef();
  const manifestForm = useRef();

  const onSubmit = (values, { setSubmitting }) => {
    if (!values.login) {
      setSubmitting(false);
      return;
    }
    const payload = JSON.stringify({
      name: `mergify-${values.login}`,
      description: `Mergify installation for ${values.login}`,
      url: 'https://mergify.com/',
      hook_attributes: {
        url: `${window.location.origin}/event`,
      },
      redirect_url: window.location.origin,
      setup_url: window.location.origin,
      // avatar: 'https://raw.githubusercontent.com/Mergifyio/mergify-engine/master/docs/source/_static/logo.png',
      public: false,
      default_permissions: {
        checks: 'write',
        contents: 'write',
        emails: 'read',
        issues: 'write',
        members: 'read',
        metadata: 'read',
        pages: 'write',
        pull_requests: 'write',
        statuses: 'read',
        administration: 'read',
        workflows: 'write',
      },
      default_events: [
        'check_run',
        'check_suite',
        'issue_comment',
        'member',
        'membership',
        'organization',
        'pull_request',
        'pull_request_review',
        'pull_request_review_comment',
        'push',
        'repository',
        'status',
        'team',
        'team_add',
      ],
    });
    manifestInput.current.value = payload;
    manifestForm.current.action = `https://github.com/organizations/${values.login}/settings/apps/new`;
    manifestForm.current.submit();
    setSubmitting(false);
  };

  return (
    <>
      <Formik onSubmit={onSubmit} initialValues={{ login: '' }}>
        {(formik) => (
          <Form onSubmit={formik.handleSubmit}>
            <Modal.Body>
              <Steper step={1} />
              <Form.Group controlId="org">
                <Form.Label>
                  GitHub Organization login where to create and install the GitHub App:
                </Form.Label>
                <Form.Control
                  as="input"
                  name="login"
                  disabled={formik.isSubmitting}
                  onChange={formik.handleChange}
                  onBlur={formik.handleBlur}
                  value={formik.values.login}
                />
              </Form.Group>
            </Modal.Body>
            <Modal.Footer>
              <span className="text-muted"><small>You will be redirected to github.com to confirm the creation.</small></span>
              <Button variant="primary" type="submit" disabled={formik.isSubmitting}>Create</Button>
            </Modal.Footer>
          </Form>
        )}
      </Formik>

      <Form action="" method="post" ref={manifestForm}>
        <input type="hidden" name="manifest" id="manifest" ref={manifestInput} />
      </Form>
    </>
  );
};

const StepFinished = function StepFinished() {
  return (
    <>
      <Modal.Body>
        <Steper step={3} />
        <Alert variant="success">The Mergify GitHub App has been created and installed successfully.</Alert>
      </Modal.Body>
      <Modal.Footer />
    </>
  );
};

const StepWaitingGitHubAppConfig = function StepWaitingGitHubAppConfig() {
  return (
    <>
      <Modal.Body>
        <Steper step={2} />
        <Container className="text-center p-3">
          <Spinner animation="border" role="status">
            <span className="visually-hidden">Loading...</span>
          </Spinner>
        </Container>
      </Modal.Body>
      <Modal.Footer />
    </>
  );
};

const StepDownloadConfig = function StepDownloadConfig(props) {
  const { info } = props;

  const downloadLink = useRef();
  const downloadBtn = useRef();
  const [downloadUrl, setDownloadUrl] = useState('');

  useEffect(() => {
    if (info.config) { downloadBtn.current.click(); }
  }, [info]);

  useEffect(() => {
    if (downloadUrl) {
      downloadLink.current.click();
      URL.revokeObjectURL(downloadUrl);
      setDownloadUrl('');
    }
  }, [downloadUrl]);

  const onDownload = () => {
    const blob = new Blob([info.config]);
    setDownloadUrl(URL.createObjectURL(blob));
  };

  return (
    <>
      <Modal.Body>
        <Steper step={2} />
        <p>
          The Mergify GitHub App has been created and the engine configuration has been downloaded.
          <br />
          You must now install the GitHub App on your organization
        </p>
      </Modal.Body>
      <Modal.Footer>
        <Button onClick={onDownload} ref={downloadBtn} variant="secondary" className="me-auto">Download the configuration</Button>
        <span className="text-muted"><small>You will be redirected to github.com to confirm the installation.</small></span>
        <a className="d-none" download="mergify.env" href={downloadUrl} ref={downloadLink}>Download link</a>
        <Button variant="primary" href={`${info.app.html_url}/installations/new/permissions?target_id=${info.app.owner.id}`}>Install</Button>
      </Modal.Footer>
    </>
  );
};
StepDownloadConfig.propTypes = {
  info: PropTypes.string.isRequired,
};

const Steps = function Steps() {
  const location = useLocation();
  const searchParams = new URLSearchParams(location.search);
  const code = searchParams.get('code') || '';
  const setupAction = searchParams.get('setup_action') || '';

  const [info, error] = useAppCreator(code);

  if (error) {
    return (
      <Container className="text-center p-3">
        <Alert variant="danger">{error}</Alert>
      </Container>
    );
  }

  if (setupAction) {
    return (<StepFinished />);
  } if (!code) {
    return (<StepCreateGitHubApp />);
  } if (!info) {
    return (<StepWaitingGitHubAppConfig />);
  }
  return (<StepDownloadConfig info={info} />);
};

const App = function App() {
  return (
    <>
      <Helmet title="Mergify">
        <meta name="description" content="Merge your code efficiently" />
        <link rel="icon" type="image/png" href={Favicon} sizes="16x16" />
        <link href="https://fonts.googleapis.com/css2?family=Poppins&display=swap" rel="stylesheet" />
      </Helmet>

      <BrowserRouter>
        <Modal.Dialog size="lg">
          <Modal.Header style={{ backgroundColor: '#C9E7F8' }}>
            <img src={LogoMergify} alt="mergify logo" width="140px" className="me-auto d-inline-block" />
            <Modal.Title>GitHub App Installer</Modal.Title>
          </Modal.Header>
          <Steps />
        </Modal.Dialog>
      </BrowserRouter>
    </>
  );
};

export default App;
