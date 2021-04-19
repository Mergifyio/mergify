import { request } from '@octokit/request';
import PropTypes from 'prop-types';
import React, { useState, useEffect, useRef } from 'react';
import {
  Button, Form, Modal, Container, Spinner, Alert, Breadcrumb,
} from 'react-bootstrap';
import { Helmet } from 'react-helmet';
import { FormProvider, useForm } from 'react-hook-form';
import {
  BrowserRouter as Router,
  Switch,
  useLocation,
} from 'react-router-dom';

import Favicon from './favicon.ico';
import LogoMergify from './logo-mergify-black.png';

import './App.css';

function getRandomString(size) {
  const randomBytes = new Uint8Array(size / 2);
  (window.crypto || window.msCrypto).getRandomValues(randomBytes);
  return Array.from(randomBytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
}

function useAppCreator(code) {
  const [app, setApp] = useState(null);
  const [config, setConfig] = useState(null);
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
      setApp(respApp.data);
      setConfig(`
MERGIFYENGINE_BASE_URL=${window.location.origin}
MERGIFYENGINE_INTEGRATION_ID=${respApp.data.id}
MERGIFYENGINE_BOT_USER_ID=${respBot.data.id}
MERGIFYENGINE_OAUTH_CLIENT_ID=${respApp.data.client_id}
MERGIFYENGINE_OAUTH_CLIENT_SECRET=${respApp.data.client_secret}
MERGIFYENGINE_WEBHOOK_SECRET=${respApp.data.webhook_secret}
MERGIFYENGINE_PRIVATE_KEY=${Buffer.from(respApp.data.pem).toString('base64')}
MERGIFYENGINE_CACHE_TOKEN_SECRET=${getRandomString(42)}
`);
    };
    run();
  }, [code, setApp]);

  return [app, config, error];
}

function Title(props) {
  const { children } = props;
  return (
    <Modal.Header style={{ backgroundColor: '#C9E7F8' }}>
      <img src={LogoMergify} alt="mergify logo" width="140px" className="mr-auto d-inline-block" />
      <Modal.Title>{children}</Modal.Title>
    </Modal.Header>
  );
}
Title.propTypes = {
  children: PropTypes.oneOfType(
    [PropTypes.string, PropTypes.element],
  ).isRequired,
};

function Steper(props) {
  const { step } = props;
  return (
    <Breadcrumb>
      <Breadcrumb.Item active={step === 1}>1. Create GitHubApp</Breadcrumb.Item>
      <Breadcrumb.Item active={step === 2}>2. Install GitHubApp </Breadcrumb.Item>
      <Breadcrumb.Item active={step === 3}>
        {window.location.hostname.endsWith('herokuapp.com')
          ? '3. Configure Heroku'
          : '3. Configure the container environment variables'}
      </Breadcrumb.Item>
    </Breadcrumb>
  );
}
Steper.propTypes = {
  step: PropTypes.number.isRequired,
};

function Step1() {
  const formMethods = useForm();
  const { watch, register, handleSubmit } = formMethods;

  const manifestInput = useRef();
  const manifestForm = useRef();
  const login = watch('login');

  const onSubmit = (e) => {
    if (!login) {
      e.preventDefault();
      return;
    }
    const payload = JSON.stringify({
      name: `mergify-${login}`,
      description: `Mergify installation for ${login}`,
      url: 'https://mergify.io/',
      hook_attributes: {
        url: `${window.location.origin}/events`,
      },
      redirect_url: window.location.origin,
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
    manifestForm.current.submit();
  };

  return (
    <>
      <Title>Creation of the GitHub App</Title>
      <FormProvider {...formMethods}>
        <Form onSubmit={handleSubmit(onSubmit)}>
          <Modal.Body>
            <Steper step={1} />
            <Form.Group controlId="org">
              <Form.Label>GitHub Organization name:</Form.Label>
              <Form.Control as="input" ref={register()} name="login" />
            </Form.Group>
          </Modal.Body>
          <Modal.Footer>
            <span className="text-muted"><small>You will be redirect to github.com to confirm the creation.</small></span>
            <Button variant="primary" type="submit">Create</Button>
          </Modal.Footer>
        </Form>
      </FormProvider>

      <Form action={`https://github.com/organizations/${login}/settings/apps/new`} method="post" ref={manifestForm}>
        <input type="hidden" name="manifest" id="manifest" ref={manifestInput} />
      </Form>
    </>
  );
}

function Step2(props) {
  const { appHtmlUrl, setInstalled } = props;
  return (
    <>
      <Title>Installation of the GitHub App on your organization</Title>
      <Modal.Body>
        <Steper step={2} />
        <p>The Mergify GitHub App have been created, you must install it to your organization</p>
      </Modal.Body>
      <Modal.Footer>
        <Button href={appHtmlUrl} target="_blank">Install</Button>
        <Button variant="secondary" onClick={() => setInstalled(true)}>Next</Button>
      </Modal.Footer>
    </>
  );
}
Step2.propTypes = {
  appHtmlUrl: PropTypes.string.isRequired,
  setInstalled: PropTypes.func.isRequired,
};

function Step2Loading() {
  return (
    <>
      <Title>Configuration of heroku</Title>
      <Modal.Body>
        <Steper step={2} />
        <Container className="text-center p-3">
          <Spinner animation="border" role="status">
            <span className="sr-only">Loading...</span>
          </Spinner>
        </Container>
      </Modal.Body>
      <Modal.Footer />
    </>
  );
}

function Step3(props) {
  const { config } = props;

  const downloadLink = useRef();
  const downloadBtn = useRef();
  const [downloadUrl, setDownloadUrl] = useState('');

  useEffect(() => {
    if (config) { downloadBtn.current.click(); }
  }, [config]);

  useEffect(() => {
    if (downloadUrl) {
      downloadLink.current.click();
      URL.revokeObjectURL(downloadUrl);
      setDownloadUrl('');
    }
  }, [downloadUrl]);

  const onDownload = () => {
    const blob = new Blob([config]);
    setDownloadUrl(URL.createObjectURL(blob));
  };

  let configMessage = config;
  if (window.location.hostname.endsWith('herokuapp.com')) {
    const appName = window.location.hostname.slice(0, -14);
    configMessage = `heroku config:set -a ${appName} ${config.split('\n').join(' ')}`;
  }

  return (
    <>
      <Title>
        {window.location.hostname.endsWith('herokuapp.com')
          ? 'Configuration of Heroku'
          : 'Configuration of the container environment variables'}
      </Title>
      <Modal.Body>
        <Steper step={3} />
        <p>
          The Mergify GitHub App have been created and configured.
          Now we need to configure the heroku app
        </p>
        <p>The following configuration variables must be set:
          <pre className="p-1" style={{ whiteSpace: 'pre-wrap' }}><code>{`${configMessage}`}</code></pre>
        </p>
        <p className="text-muted">Once set you can click finish.</p>
      </Modal.Body>
      <Modal.Footer>
        <a className="d-none" download="mergify.env" href={downloadUrl} ref={downloadLink}>Download link</a>
        <Button onClick={onDownload} ref={downloadBtn} variant="secondary">Backup the configuration</Button>
        <Button href={`${window.location.origin}/installation`}>Finish</Button>
      </Modal.Footer>
    </>
  );
}
Step3.propTypes = {
  config: PropTypes.string.isRequired,
};

function Steps() {
  const location = useLocation();
  const searchParams = new URLSearchParams(location.search);
  const code = searchParams.get('code') || '';

  const [installed, setInstalled] = useState(false);
  const [app, config, error] = useAppCreator(code);

  if (error) {
    return (
      <Container className="text-center p-3">
        <Alert variant="danger">{error}</Alert>
      </Container>
    );
  }

  if (!code) {
    return (<Step1 />);
  } if (!app) {
    return (<Step2Loading />);
  } if (!installed) {
    return (<Step2 appHtmlUrl={app.html_url} setInstalled={setInstalled} />);
  }
  return (<Step3 config={config} />);
}

// TODO(sileht): Maybe add router for /installation, so if we reach it, that
// mean heroku is not yet configured
function App() {
  return (
    <>
      <Helmet title="Mergify">
        <meta name="description" content="Merge your code efficiently" />
        <link rel="shortcut icon" type="image/png" href={Favicon} sizes="16x16" />
        <link href="https://fonts.googleapis.com/css2?family=Poppins&display=swap" rel="stylesheet" />
      </Helmet>

      <Router>
        <Switch>
          <Modal.Dialog size="lg">
            <Steps />
          </Modal.Dialog>
        </Switch>
      </Router>
    </>
  );
}

export default App;
