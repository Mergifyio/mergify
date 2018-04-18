angular.module('triage', [
    'ngRoute',
    'triage.controllers',
    'angularMoment',
    'ng-showdown',
    'LocalForageModule',
    'classy',
])

.config(['$routeProvider', '$locationProvider', '$showdownProvider', function ($routeProvider, $locationProvider, $showdownProvider) {
    $locationProvider.html5Mode(true);
    $showdownProvider.setOption('flavor', 'github');
    $routeProvider.when('/', {
        templateUrl: "/static/table.html",
        controller: 'PullsController'
    });
}])

;
