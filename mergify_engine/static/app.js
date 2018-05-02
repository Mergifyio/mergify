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
    $routeProvider
        .when('/:login', {
            templateUrl: "/static/table.html",
            controller: 'PullsController'
        })
        .when('/:login/:repo', {
            templateUrl: "/static/table.html",
            controller: 'PullsController'
        }).otherwise({
            controller : function(){
                window.location.replace('https://mergify.io/');
            },
            template : "<div></div>"
        });

}])

;
